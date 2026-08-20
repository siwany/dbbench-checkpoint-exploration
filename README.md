# DBBench Checkpointed Exploration

Two execution modes for DBBench modification tasks:

- **No exploration:** The agent executes modification queries (INSERT, UPDATE, and DELETE) linearly without rollback.
- **Checkpointed exploration:** Before each modification, the agent saves the current database state, executes the query, and inspects the result with SELECT; if the result is incorrect, it restores the latest checkpoint and tries an alternative action.

## 1. Prerequisites

Make sure Python 3.9+, Conda, Docker Desktop, and Docker Compose are installed before you follow this.

Clone this repository:

```
git clone https://github.com/siwany/dbbench-checkpoint-exploration.git
cd dbbench-checkpoint-exploration
```

## 2. Setup

```
cd AgentBench
```
Follow Steps 1 and 2 in the AgentBench [Installation Guide](https://github.com/THUDM/AgentBench#quick-start-1) to install the required dependencies and configure your OpenAI API key. Then, follow the [Docker Compose Quick Start](https://github.com/THUDM/AgentBench#quick-start) to build the required images and start the AgentBench services.


Verify that the controller is running:
```
curl -X POST http://127.0.0.1:5020/api/sync_all
```


Next, return to the project root and enter the AgentRL directory. Follow its README to install the required dependencies, then set your OpenAI API key.
You can either do
#### Option 1. Export the API Key

This sets the key for the current terminal session:

```
cd ../AgentRL
export OPENAI_API_KEY="YOUR_OPENAI_API_KEY"
```

#### Optin 2. Use an `.env` file

Create an `.env` file inside the `AgentRL` directory:

```
OPENAI_API_KEY=YOUR_OPENAI_API_KEY # GPT Models
OPENROUTER_API_KEY=YOUR_OPENROUTER_API_KEY # Qwen Models
```

Load the variables before running the evaluation:

```
cd AgentRL
set -a
source .env
set +a
```

Confirm that the key was loaded:
```
test -n "$OPENAI_API_KEY" && echo "API key loaded"
```

## 3. Run No Exploration

Run the following command from the project root directory:

```
cd AgentBench

docker compose -f extra/docker-compose.yml run --rm \
  dbbench-std \
  --controller http://controller:5020/api \
  dbbench-dev-mod-noexplore

```

Open a second terminal and run the evaluation client:

```

conda activate agent-bench
cd AgentRL

python examples/eval/server_agent.py \
  --mode no_exploration \
  -m gpt-5 \
  -u https://api.openai.com/v1 \
  -c http://127.0.0.1:5020/api \
  -t 1.0 \
  -j 1 \
  -n 1 \
  -o results/noexplore-full-gpt-5 \
  dbbench-dev-mod-noexplore
```
* `-m`: Model name
* `-u`: API base URL
* `-c`: AgentBench controller API address
* `-t`: Model temperature (default: 0.8)
* `-j`: Number of tasks to run simulataneously
* `-n`: Number of runs per task
* `-o`: Evaluation trajectories directoy
* `dbbench-dev-mod-noexplore`: DBBench task configuration to run (Available task configurations are defined in `AgentBench/configs/tasks/dbbench.yaml`.)

## 4. Run Checkpointed Exploration

Run the following command from the project root directory:

```
cd AgentBench

docker compose -f extra/docker-compose.yml run --rm \
  dbbench-std \
  --controller http://controller:5020/api \
  dbbench-dev-mod-checkpoint
```

In another terminal, run:

```
conda activate agent-bench
cd AgentRL

python examples/eval/server_agent.py \
  --mode checkpoint \
  -m gpt-4o \
  -u https://api.openai.com/v1 \
  -c http://127.0.0.1:5020/api \
  -t 0 \
  -j 1 \
  -n 1 \
  -o results/checkpoint-full \
  dbbench-dev-mod-checkpoint
```

The `--mode` argument must match the task:
* `--mode no_exploration`: dbbench-dev-mod-noexplore
* `--mode checkpoint`: dbbench-dev-mod-checkpoint

Evaluation trajectories are saved as JSONL files under the directory specified by `-o`.

### Debug Checkpoint Trajectories

To identify tasks that called `restore_checkpoint`, run:

```
export RESULT=<RESULT_PATH>

python - "$RESULT" <<'PY'
import json
import sys

with open(sys.argv[1]) as f:
    for line in f:
        row = json.loads(line)

        for message in row.get("messages", []):
            for call in message.get("tool_calls", []):
                function = call.get("function", {})

                if function.get("name") == "restore_checkpoint":
                    print("Task:", row["task_index"])
                    print("Result:", row["result"])
                    print("Restore:", function.get("arguments"))
                    print()
PY
```

To inspect the complete trajectory of a specific task, replace `TASK_ID` with its task index:
```
export TASK_ID=0

python - "$RESULT" "$TASK_ID" <<'PY'
import json
import sys

path = sys.argv[1]
target = int(sys.argv[2])

with open(path) as f:
    for line in f:
        row = json.loads(line)

        if row["task_index"] != target:
            continue

        for message in row.get("messages", []):
            print(f"\n[{message.get('role')}] {message.get('content', '')}")

            for call in message.get("tool_calls", []):
                function = call["function"]
                print(
                    f"[TOOL CALL] {function['name']}: "
                    f"{function.get('arguments')}"
                )
        break
PY
```

When debugging, inspect the sequence of modification, verification, and restoration:

```
INSERT / UPDATE / DELETE
SELECT
restore_checkpoint
SELECT
```

#### ⚠️ Pro Tip
Use a targeted `SELECT` with a WHERE condition to verify the affected row. 
A broad `SELECT *` response may be truncated, causing the agent to incorrectly assume that a successful modification failed. 
Also, you can check for blocked duplicate modification or blocked rejected modification messages to confirm that the duplicate-action guard is working.

## 5. Analyze Evaluation Results

After the evaluation is complete, use `analyze_dbbench.py` to summarize the generated JSONL trajectory. 
Evaluation results are saved under `AgentRL/results/`.

```
cd AgentRL
python examples/eval/analyze_dbbench.py \
  <RESULT_PATH> \
  > <OUTPUT_PATH>
```

For example, for no exploration:
```
python examples/eval/analyze_dbbench.py \
  results/noexplore-full/dbbench-dev-mod-noexplore-gpt-4o-mini-0.0-08201959.jsonl \
  > results/noexplore-full/gpt4o-mini-no-exploration-results.txt
```

## 6. Rerun an Experiment

Before rerunning an experiment, stop the existing task worker. 
If a worker container remains after an interrupted run, remove it manually:

```
docker rm -f dbbench-noexplore-worker 2>/dev/null || true
docker rm -f dbbench-checkpoint-worker 2>/dev/null || true
```

## 7. Stop the Environment

After completing the experiments, stop all Docker Compose services:

```
cd AgentBench
docker compose -f extra/docker-compose.yml down
```

