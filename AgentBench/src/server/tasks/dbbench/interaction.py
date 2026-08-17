import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from typing import Optional, Union, Sequence, Dict, Any, List, Tuple

import mysql.connector.aio as mysql_connector
from agentrl.worker.environment import EnvironmentController

from .environment import TYPE_MYSQL, TYPE_SQLITE, DBBenchEnvironmentDelegation


class Database:

    def __init__(self, db_type: str):
        self.logger = logging.getLogger(__name__)
        self.type = db_type

    async def initialize(self):
        raise NotImplementedError

    async def delete(self):
        raise NotImplementedError

    async def execute(self, sql: str, data: Union[Sequence, Dict[str, Any]] = ()) -> str:
        raise NotImplementedError

    async def batch_execute(self, sql: List[Union[str, Tuple[str, Union[Sequence, Dict[str, Any]]]]]):
        raise NotImplementedError


class MySQLDatabase(Database):

    def __init__(self, env_controller: EnvironmentController, checkpoint_mode: bool = False):
        super().__init__(TYPE_MYSQL)

        self.env_controller = env_controller
        assert isinstance(self.env_controller.delegation, DBBenchEnvironmentDelegation)
        self.session_id: Optional[str] = None
        self.container_id: Optional[str] = None
        self.container_ip: Optional[str] = None

        self._conn: Optional[mysql_connector.MySQLConnectionAbstract] = None
        self.password = self.env_controller.delegation.password
        self.database: Optional[str] = None
        self.checkpoint_mode = checkpoint_mode
        self.transaction_active = False
        self.current_checkpoint: Optional[str] = None
        self.checkpoint_count = 0
        self.restore_count = 0
        self.checkpoint_time_sec = 0.0
        self.restore_time_sec = 0.0

    async def initialize(self):
        session_id, container_ids, container_ips = await self.env_controller.start_session(self.type)
        self.session_id = session_id
        self.container_id = container_ids[self.type]
        self.container_ip = container_ips[self.type]
        await self._create_database()

    async def delete(self):
        if self._conn and self.transaction_active:
            try:
                await self._conn.rollback()
            except:
                self.logger.warning("Error rolling back checkpoint transaction", exc_info=True)
            self.transaction_active = False

        if self.database:
            # try to connect and delete the database
            try:
                conn = await self._get_conn()
                async with await conn.cursor() as cursor:
                    await cursor.execute(f'DROP DATABASE IF EXISTS {self.database}')
                await conn.commit()
            except:
                self.logger.warning(f'Error dropping MySQL database {self.database}:', exc_info=True)
            self.database = None

        if self._conn:
            try:
                await self._conn.close()
            except:
                self.logger.warning(f'Error closing MySQL connection:', exc_info=True)
            self._conn = None

        if self.session_id:
            try:
                await self.env_controller.end_session(self.session_id)
            except:
                self.logger.warning(f'Error ending environment session {self.session_id}:', exc_info=True)
            self.session_id = None
            self.container_id = None
            self.container_ip = None

    async def execute(self, sql: str, data: Union[Sequence, Dict[str, Any]] = ()) -> str:
        conn = await self._get_conn()
        try:
            async with await conn.cursor() as cursor:
                results = []
                await cursor.execute(sql, data)
                if cursor.with_rows:
                    rows = await cursor.fetchall()
                    results.extend(rows)
                result_str = str(results)
            if not self.checkpoint_mode:
                await conn.commit()
        except Exception as e:
            result_str = str(e)
            self.logger.error(f"MySQL Error during execution\nSQL: {sql}", exc_info=e)
            # In checkpoint mode, preserve the accepted prefix and only undo
            # the most recent tentative modification.
            try:
                if self.checkpoint_mode and self.current_checkpoint:
                    await self.restore_checkpoint()
                else:
                    await conn.rollback()
            except:
                self.logger.exception(f"Rollback failed")
        # Truncate
        if len(result_str) > 800:
            result_str = result_str[:800] + "[TRUNCATED]"
        return result_str

    async def batch_execute(self, sql: List[Union[str, Tuple[str, Union[Sequence, Dict[str, Any]]]]]):
        conn = await self._get_conn()
        try:
            async with await conn.cursor() as cursor:
                for item in sql:
                    if isinstance(item, str):
                        query = item
                        data = ()
                    else:
                        query, data = item
                    if query.strip():
                        await cursor.execute(query, data)
            await conn.commit()
        except Exception as e:
            self.logger.error(f"MySQL Error during batch execution", exc_info=e)
            try:
                await conn.rollback()
            except:
                self.logger.exception(f"Rollback failed")
            raise e

    async def create_checkpoint(self) -> Tuple[str, float]:
        """Create a SAVEPOINT before INSERT/UPDATE/DELETE."""
        if not self.checkpoint_mode:
            raise RuntimeError("Checkpointing is disabled for this database session.")

        conn = await self._get_conn()
        if not self.transaction_active:
            await conn.start_transaction()
            self.transaction_active = True

        self.checkpoint_count += 1
        checkpoint = f"cp_{self.checkpoint_count}"
        started = time.perf_counter()
        async with await conn.cursor() as cursor:
            await cursor.execute(f"SAVEPOINT {checkpoint}")
        elapsed = time.perf_counter() - started

        self.current_checkpoint = checkpoint
        self.checkpoint_time_sec += elapsed
        self.logger.info("Created checkpoint %s in %.6fs", checkpoint, elapsed)
        return checkpoint, elapsed

    async def restore_checkpoint(self) -> Tuple[str, float]:
        """Restore the most recently created SAVEPOINT."""
        if not self.checkpoint_mode or not self.current_checkpoint:
            raise RuntimeError("No checkpoint is available to restore.")

        conn = await self._get_conn()
        checkpoint = self.current_checkpoint
        started = time.perf_counter()
        async with await conn.cursor() as cursor:
            await cursor.execute(f"ROLLBACK TO SAVEPOINT {checkpoint}")
        elapsed = time.perf_counter() - started

        self.restore_count += 1
        self.restore_time_sec += elapsed
        self.logger.info("Restored checkpoint %s in %.6fs", checkpoint, elapsed)
        return checkpoint, elapsed

    async def commit_checkpointed_changes(self):
        """Commit all accepted changes when the agent submits its final answer."""
        if self.checkpoint_mode and self.transaction_active:
            conn = await self._get_conn()
            await conn.commit()
            self.transaction_active = False
            self.current_checkpoint = None

    def checkpoint_metrics(self) -> dict:
        return {
            "checkpoint_count": self.checkpoint_count,
            "restore_count": self.restore_count,
            "checkpoint_time_sec": self.checkpoint_time_sec,
            "restore_time_sec": self.restore_time_sec,
        }

    async def _create_database(self):
        database = f'dbbench_{self.session_id.replace("-", "_")}'
        conn = await self._get_conn()
        async with await conn.cursor() as cursor:
            await cursor.execute(f'CREATE DATABASE {database}')
        self.database = database
        await self._get_conn()

    async def _get_conn(self) -> mysql_connector.MySQLConnectionAbstract:
        if self._conn:
            try:  # reuse if conn is still valid
                if await self._conn.get_database() == self.database:
                    return self._conn
                else:
                    self.logger.info("Cannot reuse MySQL connection, reconnecting...")
            except:
                self.logger.warning("MySQL connection check failed, reconnecting...", exc_info=True)
            try:
                await self._conn.close()
            except:
                pass
            self._conn = None

        max_tries = 5
        for attempt in range(max_tries):
            try:
                self.logger.info(f"Connecting to MySQL at {self.container_ip} (Attempt {attempt + 1}/{max_tries})...")
                self._conn = await mysql_connector.connect(
                    host=self.container_ip,
                    user='root',
                    password=self.password,
                    database=self.database,
                    connection_timeout=10,
                    read_timeout=30,
                    write_timeout=30
                )
                return self._conn
            except Exception as e:
                self.logger.error(f"MySQL connection error: {e}")
                if attempt < max_tries - 1:
                    await asyncio.sleep(1)

        raise ConnectionError("Failed to connect to MySQL after multiple attempts.")


class SQLiteDatabase(Database):

    def __init__(self, sqlite_path: str):
        super().__init__(TYPE_SQLITE)
        self.sqlite_path = sqlite_path

    async def initialize(self):
        # check if the SQLite database file exists
        if not os.path.exists(self.sqlite_path):
            raise FileNotFoundError(f"SQLite database file not found: {self.sqlite_path}")

    async def delete(self):
        pass

    async def execute(self, sql: str, data: Union[Sequence, Dict[str, Any]] = ()) -> str:
        """使用单独的Python进程执行SQLite查询"""
        try:
            # 将查询参数转换为JSON字符串
            params_json = json.dumps(data) if isinstance(data, dict) else json.dumps(list(data))

            # 创建内嵌的Python脚本代码，直接包含SQLite查询逻辑
            python_code = f'''
import sqlite3
import json
import sys

try:
    # 解析参数
    params = json.loads('{params_json}') if '{params_json}' else ()
    
    # 连接数据库
    conn = sqlite3.connect('{self.sqlite_path}', timeout=10.0)
    cursor = conn.cursor()
    
    # 执行查询
    try:
        cursor.execute({repr(sql)}, params)
        try:
            result = cursor.fetchall()
        except sqlite3.OperationalError as e:
            if "no results" in str(e).lower():
                conn.commit()
                result = []
            else:
                conn.rollback()
                raise
        result_str = str(result)
    except Exception as e:
        error_msg = f"SQLite execution error: {{str(e)}}"
        print(error_msg, file=sys.stderr)
        try:
            conn.rollback()
        except:
            pass
        result_str = error_msg
    finally:
        cursor.close()
        conn.close()

except Exception as e:
    error_msg = f"SQLite process error: {{str(e)}}"
    print(error_msg, file=sys.stderr)
    result_str = error_msg

# 截断过长的结果并输出
if len(result_str) > 800:
    result_str = result_str[:800] + "[TRUNCATED]"
print(result_str)
'''

            # 使用子进程执行内嵌代码
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-c", python_code,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            # 等待进程完成，设置超时
            try:
                await asyncio.wait_for(process.wait(), timeout=20)
                returncode = process.returncode

                if returncode == 0:
                    # 正常完成，读取结果
                    stdout = await process.stdout.read()
                    return stdout.decode('utf-8', errors='replace').strip()
                else:
                    # 进程异常退出
                    stderr = await process.stderr.read()
                    error_text = stderr.decode('utf-8', errors='replace') if stderr else "Unknown subprocess error"
                    self.logger.error(f"SQLite subprocess failed: {error_text}")
                    return f"Error: SQLite process failed with return code {returncode}"

            except asyncio.TimeoutError:
                # 超时，终止进程
                self.logger.error(f"SQLite subprocess timed out. Terminating...")
                try:
                    process.terminate()
                    # 简短等待后检查是否需要强制终止
                    await asyncio.sleep(0.5)
                    if process.returncode is None:
                        self.logger.warning("Process didn't terminate gracefully, killing...")
                        process.kill()
                except Exception as kill_err:
                    self.logger.error(f"Error terminating process: {kill_err}")
                return f"Error: SQLite query execution timed out after 20 seconds"

        except Exception as e:
            error_msg = f"SQLite executor error: {str(e)}"
            self.logger.error(error_msg)
            return error_msg

    async def batch_execute(self, sql: List[Union[str, Tuple[str, Union[Sequence, Dict[str, Any]]]]]):
        final_sql = ''
        final_data = []
        for item in sql:
            if isinstance(item, str):
                query = item
                data = ()
            else:
                query, data = item
            if query.strip():
                if final_sql:
                    final_sql += ';\n'
                final_sql += query
                final_data.extend(data)
        await self.execute(final_sql, final_data)
