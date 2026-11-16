# Current local AIAC

Single development entry: `G:\安装\claude\work\aiac`.
Writable remote: `latent-operator/worldquant-alpha-aiac`; upstream application
baseline: `lavender1203/worldquant-alpha-aiac` at `8ffda431d08703b7f6ae13c8287806fd76eb63e0`.
Application code follows that reinstalled version, not the retired fork's research framework.

Runtime: `G:\安装\claude\runtime\aiac-clean`. Read `current.json` for the actual
source commit and interpreter; `previous.json` is the immediate code rollback.
UI http://127.0.0.1:5174; API http://127.0.0.1:8003. Existing PostgreSQL
`aiac-clean-db` and Redis `aiac-clean-redis` remain in place. The release pointer controls
one bounded native worker; beat remains off. Read the pointer/health for actual enablement.

Private configuration stays in runtime `.env` and encrypted database records.
The original Python environment is retained while referenced by current/rollback pointers.
The local MCP exposes native read APIs. Bounded create/start/report controls use
`<current.source>/ops/research.py` with the pointer's interpreter. No new orchestration service.

Research goal AIAC-PPA-QUALIFIED-ALPHA remains OPEN. Current task1/run1 produced
four actual WQ results, zero quality PASS, zero PPA_READY; task PAUSED, run COMPLETED.
Stop the receivables/revenue family. The next price-volume contrast manifest and its
dispatch/result receipts are kept in `runtime/aiac-clean/research-20261116-pv-crowding`;
read those receipts and the current report for its actual execution state.
Prior receipts and exclusion manifest are in `research-20261116-fundamental72`.

Frozen historical evidence stays in read-only `archive_production`/`archive_lab`
and private `runtime/aiac-clean/archive-20261116`. Legacy task12 is paused;
task14/run28 is isolated and cannot resume; unresolved requests are not resolved by cleanup.
Full old Git history, dirty worktrees and exported Beads context are archived in
`G:\安装\claude\backups\aiac-cleanup-20261116`. The retired goal tools are not required
to start work. PR77 remains closed; only its bounded core was adapted directly into
the original application, without restoring generated builds or governance layers.

Use `ops/local.ps1 -Action Status|Start|Stop|Update|Rollback`. Update delivers committed
code after relevant local verification; remote CI is asynchronous. Data migrations are separate:
first obtain and restore-test a private backup. Code rollback never restores a database.
Only update this concise handoff when active runtime or research facts change; results stay in AIAC.

For first bounded activation, use Update -EnableWorker. Subsequent updates retain the flag.
The fixed batch is serial, at most 16 candidates within its declared create-request budget.
Before each POST the trace is REQUESTED; a durable Location becomes ACCEPTED and allows GET-only
recovery. An interrupted REQUESTED becomes UNKNOWN and is never automatically resent.
Worker startup only recovers RUNNING bounded runs, never old/paused runs. Quality and PPA remain
separate; final PPA evaluation is not implemented here. On completion the same worker runs
one advisory review through the existing LLM service (60-second cap), stores a REVIEW trace
and emits an in-app/Windows notification. Failed review preserves the batch and quality labels.
Worker startup recovers missing completed bounded reviews, without repeating WQ requests.
Desktop delivery is at most one attempt; ATTEMPTED after interruption means delivery uncertain.
Notifications are read at /api/v1/runs/bounded/notifications; report includes the saved review.
This does not wake a Codex chat or start another experiment. Windows settings may suppress toasts.
Two thin project skills live in `.agents/skills`; ordinary dispatch returns IDs and ends the chat wait.
