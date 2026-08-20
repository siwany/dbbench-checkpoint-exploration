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
You should be able to see `null`.


Next, return to the project root and enter the AgentRL directory. Follow its README to install the required dependencies, then set your OpenAI API key.

#### Option 1. Export the API Key

This sets the key for the current terminal session:

```
cd ../AgentRL
export OPENAI_API_KEY="YOUR_OPENAI_API_KEY"
```

#### Option 2. Use an `.env` file

Create an `.env` file inside the `AgentRL` directory:

```
OPENAI_API_KEY=YOUR_OPENAI_API_KEY
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
  -m <MODEL NAME> \
  -u https://api.openai.com/v1 \
  -c http://127.0.0.1:5020/api \
  -t 1.0 \
  -j 1 \
  -n 1 \
  -o results/noexplore-full \
  dbbench-dev-mod-noexplore
```
* `-m`: Model name
  * [Model List](AgentBench/configs/agents/api_agents.yaml)
* `-u`: API base URL
* `-c`: AgentBench controller API address
* `-t`: Model temperature (default: 0.8) 
  * Some reasoning models, including GPT-5 and o-series models, do not support custom temperature values; use the default value of 1.0 or omit this parameter for those models.
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
  -m <MODEL NAME> \
  -u https://api.openai.com/v1 \
  -c http://127.0.0.1:5020/api \
  -t 1 \
  -j 1 \
  -n 1 \
  -o results/checkpoint-full \
  dbbench-dev-mod-checkpoint
```

The `--mode` argument must match the task:
* `--mode no_exploration`: dbbench-dev-mod-noexplore
* `--mode checkpoint`: dbbench-dev-mod-checkpoint

Evaluation trajectories are saved as JSONL files under the directory specified by `-o`.

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

