# DBBench Checkpointed Exploration

Two execution modes for DBBench modification tasks:

- **No exploration:** The agent executes INSERT, UPDATE, and DELETE queries linearly without rollback.
- **Checkpointed exploration:** The agent creates a checkpoint before each modification and can inspect, restore, and retry database actions.

## 1. Prerequisites

Install:
- Python 3.9+
- Conda
- Docker Desktop
- Docker Compose
- An OpenAI API key

Clone this repository:

```
git clone https://github.com/siwany/dbbench-checkpoint-exploration.git
cd dbbench-checkpoint-exploration
```

## 2. Setup
Follow the official AgentBench [Quick Start](https://github.com/THUDM/AgentBench#quick-start) to install the required dependencies and start the Docker Compose services after going to the folder.

```
cd AgentBench
```

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
  -m gpt-4o-mini \
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
  -m gpt-4o-mini \
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

## 5. Analyze Evaluation Results

After the evaluation is complete, use `analyze_dbbench.py` to summarize the generated JSONL trajectory:

```
cd AgentRL

python examples/eval/analyze_dbbench.py \
  results/noexplore-full/RESULT_FILE.jsonl
```

For checkpointed exploration:

```
python examples/eval/analyze_dbbench.py \
  results/checkpoint-full/RESULT_FILE.jsonl
```
Make Sure to replace RESULT_FILE.jsonl with the actual generated filename.

To save analysis to a text file:

```
python examples/eval/analyze_dbbench.py \
  results/checkpoint-full/RESULT_FILE.jsonl \
  > results/checkpoint-full/result_analysis.txt
```

## 6. Stop the Environment

After finishing experiment, stop the environment by running:
```
cd AgentBench
docker compose -f extra/docker-compose.yml down
```
