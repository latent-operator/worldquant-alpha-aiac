# Current local AIAC

Single development entry: `G:\安装\claude\work\aiac`.
Writable remote: `latent-operator/worldquant-alpha-aiac`; upstream application
baseline: `lavender1203/worldquant-alpha-aiac` at `8ffda431d08703b7f6ae13c8287806fd76eb63e0`.
Application code follows that reinstalled version, not the retired fork's research framework.

Runtime: `G:\安装\claude\runtime\aiac-clean`. Read `current.json` for the actual
source commit and interpreter; `previous.json` is the immediate code rollback.
UI http://127.0.0.1:5174; API http://127.0.0.1:8003. Existing PostgreSQL
`aiac-clean-db` and Redis `aiac-clean-redis` remain in place. No worker or beat.

Private configuration stays in runtime `.env` and encrypted database records.
The original Python environment is retained while referenced by current/rollback pointers.
The local MCP exposes native read APIs only; obsolete legacy research endpoints and controls
are not advertised while background execution remains on hold.

Research goal AIAC-PPA-QUALIFIED-ALPHA remains OPEN. Current task1/run1 produced
four actual WQ results, zero quality PASS, zero PPA_READY; task PAUSED, run COMPLETED.
Stop the receivables/revenue family. Next price-volume contrast was prepared but NOT dispatched.
Its manifest is in `runtime/aiac-clean/research-20261116-pv-crowding`.
Prior receipts and exclusion manifest are in `research-20261116-fundamental72`.

Frozen historical evidence stays in read-only `archive_production`/`archive_lab`
and private `runtime/aiac-clean/archive-20261116`. Legacy task12 is paused;
task14/run28 is isolated and cannot resume; unresolved requests are not resolved by cleanup.
Full old Git history, dirty worktrees and exported Beads context are archived in
`G:\安装\claude\backups\aiac-cleanup-20261116`. The retired goal tools are not required
to start work. PR77/commit831ba06 is preserved for a later, narrower worker change.

Use `ops/local.ps1 -Action Status|Start|Stop|Update|Rollback`. Update delivers committed
code after relevant local verification; remote CI is asynchronous. Data migrations are separate:
first obtain and restore-test a private backup. Code rollback never restores a database.
Only update this concise handoff when active runtime or research facts change; results stay in AIAC.
