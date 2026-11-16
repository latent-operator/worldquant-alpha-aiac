---
name: aiac-research
description: Define and dispatch a bounded real WQ experiment through local AIAC when the owner asks to continue research or test a hypothesis. Use persisted results and native background execution. Not for read-only reviews, software maintenance or Alpha submission.
---

# AIAC research

Read `G:\安装\claude\work\aiac\LOCAL_CONTEXT.md` and
`G:\安装\claude\runtime\aiac-clean\current.json`. Use the pointer's `python_executable`
and `<source>/ops/research.py`; inspect `--help` and `health` before dispatch.
Do not use retired Beads/OpenSpec recovery or old fork tools.

1. Read recent persisted batch reports and exclusion evidence. Choose an economic mechanism;
   repeated weak families require a documented change or stop, not a post-hoc sign flip.
2. Verify field IDs for the actual WQ dataset/region/universe/delay and retain the source/time.
   Separate live catalog coverage from unproven PIT/accounting assumptions. The backend validates
   declared fields and syntax, not live metadata semantics. Community posts are hypotheses, not platform rules.
3. Freeze exact candidates, settings, request budget and stop conditions in the task-create JSON.
   Use the [manifest example](manifest.md). Preserve comparable settings across contrasts.
4. Run `create --manifest PRIVATE_PATH`, retain its task ID, then `start --task-id ID` once.
   The owner authorizes real backtests by default for research requests; no repeated start confirmation.
   A timeout requires reading task/run state before retrying creation or start.
5. Return actual task/run IDs, budget and `http://127.0.0.1:5174/tasks/ID`, then end ordinary dispatch.
   Celery owns the wait. Do not keep a chat or browser polling. Monitor to completion only when requested.
   Completion automatically saves a review and posts AIAC/Windows notifications; it does not wake
   a Codex chat or submit an Alpha. The owner additionally authorizes an explicitly continuous task
   with no total request cap and up to 10 simultaneous simulations. Set continuous=true,
   max_concurrent, verified_fields and a stable family; each batch still freezes its request budget.
   The original worker then plans subsequent batches from saved reviews. Pause/stop halts continuation.

Use `report --run-id ID` for later review. Explicit `resume --task-id ID` reuses the same eligible
bounded run; ACCEPTED positions resume GET only. UNKNOWN/REQUESTED/REJECTED/FAILED block resume.
Never replay frozen legacy signatures, bypass an offline worker with browser POSTs, or manipulate
the database. Stop on uncertainty/429; do not enlarge the budget silently. No Alpha submission.
Quality PASS and final PPA_READY are separate from completed execution; missing evidence stays missing.
