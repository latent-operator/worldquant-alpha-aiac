# Current local AIAC

Development: `G:\安装\claude\work\aiac`.
Runtime: `G:\安装\claude\runtime\aiac-clean`; `current.json` records the actual release.
API http://127.0.0.1:8003; UI http://127.0.0.1:5174.
Accounts and PostgreSQL/Redis volumes are unchanged. Never restore a database on code rollback.
Local relevant verification is sufficient; remote CI is asynchronous.
Use `ops/local.ps1 -Action Start|Stop|Status|Update|Rollback` for existing services.

Owner decision 2026-11-17: restore upstream `8ffda431` task service and mining worker.
Use existing MiningAgent evolution, strategy agent and batch adapter. The locally added
bounded executor, continuous planner and completion-review writer were removed.
No new scheduler, automatic cross-run chain, or chat-resident poller.
Small retirement guards prevent old bounded tasks being replayed by the native worker.
`bounded_research.py` now contains only read-only historical reports and worker health.
Task UI is upstream again; added AIAC/Windows completion notifications are no longer generated.

Use `.agents/skills/aiac-operator/SKILL.md` for native task operations, research dialogue,
Playwright community research and trace review. The workspace entry references this source.
The former aiac-research/aiac-review skills are retired. Skills do not run in the background.
Clarify missing requirements, then dispatch without a second approval. Reuse the existing Worker;
hand off within 60 seconds, return task/run IDs and end the chat wait. Review on the user's
next request, not through automatic chat wakeups. Community login failures require the user
to log in through the browser before dependent research dispatch; never request cookies.
Native evolution adjusts strategy within its configured loop, has iteration/time/dataset
stopping conditions, and uses multi-simulation by default. Do not promise unlimited
cross-run continuation or account batch permissions without evidence.
No adapter or MiningAgent rewrite was added during restoration.

Historical tasks 1-4 and runs 1-5 retain original settings, results and labels.
At retirement run5 had 10 RESULT; task4 was PAUSED and no requests remained pending.
Private receipts are under runtime/aiac-clean/continuous-runner and research-* directories.
Retired reports remain at GET /api/v1/runs/{id}/bounded; native reports use run, trace and alphas.
Legacy task12/task14/run28 remain frozen in archive_production/archive_lab and private archives.
Historical exclusions: runtime/aiac-clean/research-20261116-fundamental72/history-exclusions.json.
Do not restart archived tasks or replay UNKNOWN; consult receipts before starting native tasks.

Research goal remains OPEN: persisted quality PASS and final PPA_READY must refer to the
same Alpha. Execution completion or HTTP success is not quality success. No Alpha submission.
