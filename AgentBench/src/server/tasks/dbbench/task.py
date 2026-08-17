import asyncio
import json
import logging
import os
import weakref
from typing import Optional, List, Tuple, Sequence, Union

from agentrl.worker.environment import create_controller
from agentrl.worker.task import Task, Session
from agentrl.worker.typings import (AgentCancelledException,
                                    RewardHistoryItem,
                                    SampleIndex,
                                    SampleStatus,
                                    TaskSampleExecutionResult)
from openai.types.chat import (ChatCompletionSystemMessageParam,
                               ChatCompletionToolMessageParam,
                               ChatCompletionUserMessageParam)

from .environment import DBBenchEnvironmentDelegation, TYPE_SQLITE
from .interaction import Database, MySQLDatabase, SQLiteDatabase
from .result_processor import DBResultProcessor

SYSTEM_PROMPT = """I will ask you a question, then you should help me operate a MySQL database with SQL to answer the question.
You have to explain the problem and your solution to me and write down your thoughts.
After thinking and explaining thoroughly, every round you can choose to operate or to answer with the two specific tools provided.
If you should execute a SQL query, use the `execute_sql` function, Your SQL should be in one line.
Every time you can only execute one SQL statement. I will only execute the statement in the first SQL code block. Every time you write a SQL, I will execute it for you and give you the output.
If you are done operating, and you want to commit your final answer, then use the `commit_final_answer` function.
DO NOT use this tool unless you are sure about your answer. I expect an accurate and correct answer.
Your answer should be accurate. Your answer must be exactly the same as the correct answer.
If the question is about modifying the database, then after done operation, your answer field can be anything.
If your response cannot match any pattern I mentioned earlier, you will be judged as FAIL immediately.
You should always use the tools provided to submit your answer. Be careful not to write it in the content field.
Your input will be raw MySQL response, you have to deal with it by yourself."""

CHECKPOINT_PROMPT = """

Checkpointed exploration is enabled for this task.
Before every INSERT, UPDATE, or DELETE, the environment automatically creates a database SAVEPOINT.
After a modification, inspect the affected state with SELECT before committing the final answer.
If the modification affected the wrong rows or produced an incorrect state, call restore_checkpoint, then try an alternative SQL statement.
When calling restore_checkpoint, explain why the last modification was wrong.
Never repeat an identical modification. A restored SQL statement is rejected and must be replaced with a different strategy.
Only the most recent checkpoint can be restored. All accepted changes are committed only when you call commit_final_answer."""

MODIFICATION_OPERATIONS = {"INSERT", "UPDATE", "DELETE"}


class DBBenchTask(Task):

    def __init__(self,
                 data_file: str,
                 db_file: Optional[str] = None,
                 db_password: str = 'password',
                 max_round: int = 20,
                 env_driver: str = 'docker',
                 env_options: Optional[dict] = None,
                 mode: str = 'no_exploration',
                 **configs):
        super().__init__(**configs)
        self.full_async = True
        self.logger = logging.getLogger(__name__)

        self.max_round = max_round
        self.data_file = data_file
        self.db_root_dir = db_file
        if mode not in {'no_exploration', 'checkpoint'}:
            raise ValueError(f"Unsupported DBBench mode: {mode}")
        self.mode = mode

        self.dataset = []
        # Load dataset
        with open(self.data_file) as f:
            raw_data = f.read()
            if self.data_file.endswith("json"):
                data = json.loads(raw_data)
            else:  # Assuming jsonl
                data = [json.loads(line) for line in raw_data.strip().split('\n')]

        for entry in data:
            ans_key = "answer_md5" if entry["type"][0] in ("INSERT", "DELETE", "UPDATE") else "label"
            ans = entry.pop(ans_key, None)  # Use pop with default
            inp = entry
            self.dataset.append((inp, ans))

        self.env_delegation = DBBenchEnvironmentDelegation(db_password)
        self.env_controller = create_controller(env_driver, self.env_delegation, **env_options)
        self.env_controller_background_task = None

        self.logger.info(f"DBBench initialized with {len(self.dataset)} samples. Root dir: {self.db_root_dir}")

    def get_indices(self) -> List[SampleIndex]:
        return list(range(len(self.dataset)))

    async def start_sample(self, index: int, session: Session) -> TaskSampleExecutionResult:
        self.env_controller.loop = asyncio.get_running_loop()
        if not self.env_controller_background_task:
            self.env_controller_background_task = asyncio.create_task(self.env_controller.background_task())
            weakref.finalize(self, self.env_controller_background_task.cancel)

        database: Optional[Database] = None
        try:
            entry = self.dataset[index][0]
            ground_truth = self.dataset[index][1]

            use_sqlite = entry.get("user_sqlite", False)
            if use_sqlite:
                db_dir = entry['create']['database']
                init_file = entry['create']['init']
                sqlite_path = os.path.join(self.db_root_dir, db_dir, init_file)
                database = SQLiteDatabase(sqlite_path)
                await database.initialize()
            else:
                init_sql = self._build_init_sql(entry)
                database = MySQLDatabase(
                    self.env_controller,
                    checkpoint_mode=self.mode == 'checkpoint'
                )
                await database.initialize()
                await database.batch_execute(init_sql)

            system_prompt = SYSTEM_PROMPT
            if self.mode == 'checkpoint':
                system_prompt += CHECKPOINT_PROMPT
            session.inject(ChatCompletionSystemMessageParam(
                role='system',
                content=system_prompt
            ))

            user_prompt = ""
            if "evidence" in entry and entry['evidence'] != "":
                user_prompt += "Evidence about the question: " + entry["evidence"] + "\n"
            if "add_description" in entry and entry['add_description'] != "":
                user_prompt += "Additional table information about the question: " + entry["add_description"] + "\n"
            user_prompt += "Question: " + entry["description"] + "\n"
            session.inject(ChatCompletionUserMessageParam(
                role='user',
                content=user_prompt
            ))

            # Per-sample lightweight failure memory. This is deliberately kept
            # local so concurrent DBBench sessions cannot affect one another.
            last_modification_sql: Optional[str] = None
            last_modification_normalized: Optional[str] = None
            rejected_sqls = {}

            for current_round in range(self.max_round):
                response = await session.action()

                tool_calls = []
                for message in response.messages:
                    tool_calls.extend(message.get('tool_calls', []) or [])

                if not tool_calls:
                    if self.mode == 'checkpoint' and last_modification_sql:
                        recovery_message = (
                            'You did not call a tool. Do not repeat the previous modification. '
                            'Use SELECT to inspect the current state, call restore_checkpoint with '
                            'a reason if it is wrong, or call commit_final_answer if it is correct.'
                        )
                    else:
                        recovery_message = 'Internal error: No tool calls found despite finish reason.'
                    session.inject(ChatCompletionUserMessageParam(
                        role='user',
                        content=recovery_message
                    ))
                    continue

                for tool_call in tool_calls:
                    call_id = tool_call.get('id', '')
                    try:
                        function_name = tool_call.get('function', {}).get('name', '')
                        arguments = tool_call.get('function', {}).get('arguments', '{}')
                        arguments = json.loads(arguments)
                    except:
                        session.inject(ChatCompletionToolMessageParam(
                            role='tool',
                            tool_call_id=call_id,
                            content='Error: Failed to parse tool call. Tool call format is incorrect.'
                        ))
                        self.logger.warning(f'Error parsing tool call: {tool_call}', exc_info=True)
                        continue

                    if function_name == 'execute_sql':
                        try:
                            sql = list(arguments.values())[0]
                            operation = self._sql_operation(sql)
                            checkpoint_note = ''

                            if (
                                self.mode == 'checkpoint'
                                and operation in MODIFICATION_OPERATIONS
                                and isinstance(database, MySQLDatabase)
                            ):
                                normalized_sql = self._normalize_sql(sql)
                                if normalized_sql == last_modification_normalized:
                                    response = (
                                        'Blocked duplicate modification. This SQL was already executed. '
                                        'Do not repeat it. Use SELECT to inspect the state, '
                                        'restore_checkpoint if it is wrong, or commit_final_answer '
                                        'if it is correct.'
                                    )
                                    session.inject(ChatCompletionToolMessageParam(
                                        role='tool',
                                        tool_call_id=call_id,
                                        content=response
                                    ))
                                    continue
                                if normalized_sql in rejected_sqls:
                                    response = (
                                        'Blocked rejected modification. This SQL was previously restored '
                                        f"because: {rejected_sqls[normalized_sql]} "
                                        'Generate a different SQL strategy.'
                                    )
                                    session.inject(ChatCompletionToolMessageParam(
                                        role='tool',
                                        tool_call_id=call_id,
                                        content=response
                                    ))
                                    continue
                                checkpoint, checkpoint_sec = await database.create_checkpoint()
                                checkpoint_note = (
                                    f"\nCheckpoint {checkpoint} was automatically created before "
                                    f"this modification in {checkpoint_sec:.6f} seconds. "
                                    "Inspect the affected state with SELECT. If it is incorrect, "
                                    "call restore_checkpoint."
                                )
                            self.logger.info(f'Executing SQL: {sql}')
                            response = await asyncio.wait_for(database.execute(sql), 60)
                            if self.mode == 'checkpoint' and operation in MODIFICATION_OPERATIONS:
                                last_modification_sql = sql
                                last_modification_normalized = self._normalize_sql(sql)
                            response += checkpoint_note
                            self.logger.info(f'DB response: {response[:100]}{"..." if len(response) > 100 else ""}')
                            if not response:
                                response = 'No response from database.'
                            if 'Error' in response or 'error' in response.lower():
                                if "syntax" in response.lower():
                                    self.logger.warning(f"SQL syntax error detected: {sql}")
                        except asyncio.TimeoutError:
                            self.logger.warning(f'Timeout executing SQL: {sql}')
                            response = 'Error: SQL execution timed out.'
                        except Exception as e:
                            self.logger.exception(f'Error executing query', exc_info=True)
                            response = f'Error executing query: {e}'
                        session.inject(ChatCompletionToolMessageParam(
                            role='tool',
                            tool_call_id=call_id,
                            content=response
                        ))

                    elif function_name == 'restore_checkpoint':
                        try:
                            if self.mode != 'checkpoint' or not isinstance(database, MySQLDatabase):
                                raise RuntimeError('Checkpoint restore is not enabled for this task.')
                            if not last_modification_sql or not last_modification_normalized:
                                raise RuntimeError('No modification is available to reject and restore.')
                            reason = arguments.get('reason', '').strip()
                            if not reason:
                                raise RuntimeError('A reason is required when restoring a checkpoint.')
                            checkpoint, restore_sec = await database.restore_checkpoint()
                            rejected_sqls[last_modification_normalized] = reason
                            response = (
                                f"Restored {checkpoint} in {restore_sec:.6f} seconds. "
                                f"Rejected SQL: {last_modification_sql} Reason: {reason} "
                                "The tentative modification was undone. Do not repeat this SQL or an "
                                "equivalent action. Revise the strategy before trying again."
                            )
                        except Exception as e:
                            self.logger.warning('Checkpoint restore failed', exc_info=True)
                            response = f'Error restoring checkpoint: {e}'
                        session.inject(ChatCompletionToolMessageParam(
                            role='tool',
                            tool_call_id=call_id,
                            content=response
                        ))

                    elif function_name == 'commit_final_answer':
                        answer = list(arguments.values())[0]
                        if not answer:
                            self.logger.warning('Empty answer submitted')
                        else:
                            self.logger.info(f"Final answer submitted: {answer[:100]}{'...' if len(answer) > 100 else ''}")

                        std_sql = entry.get("sql", {}).get("query")
                        db_type = database.type

                        if self.mode == 'checkpoint' and isinstance(database, MySQLDatabase):
                            await database.commit_checkpointed_changes()

                        # Calculate hash for modification tasks
                        if entry["type"][0] in ("INSERT", "DELETE", "UPDATE"):
                            self.logger.info(f"Calculating table hash ({db_type})...")
                            if db_type == TYPE_SQLITE:
                                self.logger.warning(f"Table hash calculation for SQLite not implemented.")
                                answer_to_compare = "SQLite hash not implemented"  # Placeholder
                            else:
                                answer_to_compare = await DBResultProcessor.calculate_tables_hash_async(database, entry)

                            if ground_truth == "":
                                # 这里对于 Insert 类的操作，我们根据 std_sql 来计算 hash
                                answer_db: Optional[Database] = None
                                try:
                                    answer_db = MySQLDatabase(self.env_controller)
                                    await answer_db.initialize()
                                    init_sql = self._build_init_sql(entry)
                                    await answer_db.batch_execute(init_sql)
                                    await answer_db.execute(std_sql)
                                    ground_truth = await DBResultProcessor.calculate_tables_hash_async(answer_db, entry)
                                finally:
                                    if answer_db:
                                        await answer_db.delete()
                        else:
                            answer_to_compare = answer

                        # Compare results
                        self.logger.info(f"Final Answer: {str(answer_to_compare)[:100]}{'...' if len(str(answer_to_compare)) > 100 else ''}")
                        self.logger.info(f"Ground Truth: {str(ground_truth)[:100]}{'...' if len(str(ground_truth)) > 100 else ''}")
                        is_correct = DBResultProcessor.compare_results(answer_to_compare, ground_truth, entry["type"][0])
                        self.logger.info(f"Correct: {is_correct}")

                        session.inject(RewardHistoryItem(
                            reward=1 if is_correct else 0,
                            score=1 if is_correct else 0
                        ))
                        return TaskSampleExecutionResult(
                            status=SampleStatus.COMPLETED,
                            result={
                                "is_correct": is_correct,
                                "answer": answer,
                                "ground_truth": ground_truth,
                                "std_sql": std_sql,
                                "type": entry["type"][0],
                                "mode": self.mode,
                                "checkpoint_metrics": (
                                    database.checkpoint_metrics()
                                    if isinstance(database, MySQLDatabase)
                                    else {}
                                )
                            }
                        )

                    else:  # max rounds limit has reached
                        self.logger.warning(f"Invalid function call: {function_name}")
                        session.inject(ChatCompletionToolMessageParam(
                            role='tool',
                            tool_call_id=call_id,
                            content='Invalid function call. Please call a tool instead.'
                        ))

            else:
                session.inject(RewardHistoryItem(reward=0, score=0))
                return TaskSampleExecutionResult(status=SampleStatus.TASK_LIMIT_REACHED)

        except AgentCancelledException:
            session.inject(RewardHistoryItem(reward=0, score=0))
            return TaskSampleExecutionResult(status=SampleStatus.CANCELLED)
        except:
            self.logger.exception('Error during task execution')
            session.inject(RewardHistoryItem(reward=0, score=0))
            return TaskSampleExecutionResult(status=SampleStatus.TASK_ERROR)
        finally:
            if database:
                try:
                    await database.delete()
                except:
                    self.logger.warning('Error during database cleanup', exc_info=True)

    @staticmethod
    def _sql_operation(sql: str) -> str:
        stripped = sql.lstrip()
        return stripped.split(None, 1)[0].upper() if stripped else ''

    @staticmethod
    def _normalize_sql(sql: str) -> str:
        return ' '.join(sql.strip().rstrip(';').lower().split())

    @staticmethod
    def _build_init_sql(entry: dict) -> List[Union[str, Tuple[str, Sequence[str]]]]:
        """Builds initialization SQL for MySQL."""
        tables = entry["table"] if isinstance(entry["table"], list) else [entry["table"]]
        final_sql = []
        for table in tables:
            name = table["table_name"]
            columns = ",".join([f"`{c['name']}` TEXT" for c in table["table_info"]["columns"]])
            column_names = ",".join([f"`{c['name']}`" for c in table["table_info"]["columns"]])
            items = []
            items_data = ()
            for row in table["table_info"]["rows"]:
                item = "(" + ",".join(["%s"] * len(row)) + ")"
                items_data += tuple(str(col) for col in row)
                items.append(item)
            items_str = ",".join(items)
            final_sql.append(f'CREATE TABLE IF NOT EXISTS `{name}` ({columns})')
            final_sql.append((
                f'INSERT INTO `{name}` ({column_names}) VALUES {items_str}',
                items_data
            ))
        return final_sql