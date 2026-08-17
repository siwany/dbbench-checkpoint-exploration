import json
import statistics
import sys
from collections import Counter

path = sys.argv[1]

rows = []
with open(path, encoding="utf-8") as f:
    for line in f:
        if line.strip():
            rows.append(json.loads(line))


def extract_info(row):
    sql_actions = []
    question = ""
    internal_errors = 0

    for message in row["messages"]:
        role = message.get("role")

        if role == "user":
            content = message.get("content", "")

            if "Question:" in content:
                question = content.split("Question:", 1)[1].strip()

            if "Internal error:" in content:
                internal_errors += 1

        for call in message.get("tool_calls", []):
            function = call.get("function", {})

            if function.get("name") == "execute_sql":
                try:
                    arguments = json.loads(
                        function.get("arguments", "{}")
                    )
                    sql = arguments.get("query", "")
                except json.JSONDecodeError:
                    sql = function.get("arguments", "")

                operation = (
                    sql.strip().split()[0].upper()
                    if sql.strip()
                    else "UNKNOWN"
                )

                sql_actions.append({
                    "operation": operation,
                    "sql": sql,
                })

    modification_count = sum(
        action["operation"] in {"INSERT", "UPDATE", "DELETE"}
        for action in sql_actions
    )

    return {
        "task_index": row["task_index"],
        "success": row["result"] == 1,
        "question": question,
        "elapsed_sec": row.get("elapsed_sec"),
        "sql_actions": sql_actions,
        "sql_count": len(sql_actions),
        "modification_count": modification_count,
        "internal_errors": internal_errors,
    }


results = [extract_info(row) for row in rows]
successes = [r for r in results if r["success"]]
failures = [r for r in results if not r["success"]]
elapsed = [
    r["elapsed_sec"]
    for r in results
    if r["elapsed_sec"] is not None
]

operation_counts = Counter(
    action["operation"]
    for result in results
    for action in result["sql_actions"]
    if action["operation"] in {"INSERT", "UPDATE", "DELETE"}
)

print("===== SUMMARY =====")
print(f"Total tasks: {len(results)}")
print(f"Successful: {len(successes)}")
print(f"Failed: {len(failures)}")
print(f"Success rate: {len(successes) / len(results):.2%}")
print(f"Failed task IDs: {[r['task_index'] for r in failures]}")
print(f"Operation counts: {dict(operation_counts)}")

if elapsed:
    print(f"Average time: {statistics.mean(elapsed):.2f}s")
    print(f"Median time: {statistics.median(elapsed):.2f}s")

print(
    "Average SQL actions: "
    f"{statistics.mean(r['sql_count'] for r in results):.2f}"
)
print(
    "Average modifications: "
    f"{statistics.mean(r['modification_count'] for r in results):.2f}"
)
print(
    "Tasks with internal errors: "
    f"{sum(r['internal_errors'] > 0 for r in results)}"
)

print("\n===== FAILED TRAJECTORIES =====")

for result in failures:
    print(f"\nTask {result['task_index']}")
    print(f"Question: {result['question']}")
    print(f"Elapsed: {result['elapsed_sec']} seconds")
    print(f"Internal errors: {result['internal_errors']}")

    for number, action in enumerate(result["sql_actions"], 1):
        print(
            f"  SQL {number} [{action['operation']}]: "
            f"{action['sql']}"
        )