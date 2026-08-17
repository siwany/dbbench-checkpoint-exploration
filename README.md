# DBBench Checkpointed Exploration

Two execution modes for DBBench modification tasks:

- No exploration: The agent executes INSERT, UPDATE, and DELETE queries linearly without rollback.
- Checkpointed exploration: The agent creates a checkpoint before each modification and can inspect, restore, and retry database actions.

The experiments use 40 modification tasks from the DBBench development set, a mysql:8 Docker environment, AgentRL’s evaluation client, and GPT-4o.

## 1. Prerequisites

Install:
- Python 3.9+
- Conda
- Docker Desktop
- Docker Compose
- An OpenAI API key

Clone this repository:

```
git clone https://github.com/YOUR_USERNAME/dbbench-checkpoint-exploration.git
cd dbbench-checkpoint-exploration
```

## 2. Setup
Follow the official AgentBench [Quick Start](https://github.com/THUDM/AgentBench#quick-start) to install the required dependencies and start the Docker Compose services. 

Also install AgentRL according to its README and set your OpenAI API key:

```
export OPENAI_API_KEY="YOUR_OPENAI_API_KEY"
```

Then verify the controller:
```
curl -X POST http://127.0.0.1:5020/api/sync_all
```

## 3. Run No Exploration

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
  -m gpt-4o \
  -u https://api.openai.com/v1 \
  -c http://127.0.0.1:5020/api \
  -t 0 \
  -j 1 \
  -n 1 \
  -o results/noexplore-full \
  dbbench-dev-mod-noexplore
```

## 4. Run Checkpointed Exploration
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
* `--mode no_exploration` dbbench-dev-mod-noexplore
* `--mode checkpoint with` dbbench-dev-mod-checkpoint

Evaluation trajectories are saved as JSONL files under the directory specified by `-o`.

## 5. Stop the Environment
```
cd AgentBench
docker compose -f extra/docker-compose.yml down
```