---
name: aiac-research
description: Define and start real WQ research through local AIAC's native task API and evolution loop, using task prompts and saved evidence. Not for read-only reviews or Alpha submission.
---

# AIAC research

Read `G:\安装\claude\work\aiac\LOCAL_CONTEXT.md` and the runtime `current.json`.
Use its interpreter with `<source>/ops/research.py`, or the native task API.
The owner authorizes real backtests for research requests; do not ask again to start.

1. Read recent run/trace/Alpha records and private historical exclusions.
   Separate simulation errors from poor quality. Never replay UNKNOWN or old tasks.
2. Select a dataset with verified fields for the actual region/universe/delay.
   Specify an economic hypothesis, contrasts, failed mechanisms and stopping criteria.
3. Use existing task config: `target_hypothesis`, `constraints`, `preferred_fields`,
   `avoid_fields`, `preferred_operators`, `avoid_operators`. Pass historical expressions
   in `attempted_expressions`; inspect native dedup behavior rather than assuming this
   is an enforced global ledger. See [native request example](manifest.md).
   Do not use bounded/continuous configuration or frozen manifests.
4. POST /api/v1/tasks then POST /api/v1/tasks/{id}/start once.
   CLI: `create --manifest PRIVATE_JSON`, then `start --task-id ID`.
   On response timeout read state before retrying; never replay uncertain submissions.
5. Return actual IDs and task URL. Native Celery/MiningAgent owns execution.
   Do not keep the chat polling unless asked. Strategy evolution and dataset stopping
   govern the configured run; a Skill does not provide unlimited cross-run continuation.

Research prompt: Judge only saved WQ results. When a mechanism repeatedly fails,
change its economic explanation or dataset; do not merely rename it, flip its sign
following observed results, or search nearby parameters. Distinguish execution failure
from quality failure. Missing correlation/OS/PIT evidence remains missing.
This guides the model; it is not a deterministic execution guarantee.

Review with `report --run-id ID`. Pause/stop uses the native intervention API.
Upstream RESUME only changes status; verify whether a worker is still executing.
Do not start a duplicate worker for paused in-flight work. Retired bounded tasks
cannot start/resume; their reports remain readable.
Native defaults include 4 candidates per round, multi-simulation and finite iteration/time
limits. Do not claim 10-way throughput or unlimited execution from configuration alone.
No Alpha submission. Preserve credentials, history and saved quality labels.
