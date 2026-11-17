# Restore native mining

Goal: retire the locally added bounded executor and continuous planner; use the
existing AIAC MiningAgent evolution loop and task configuration prompts.

1. Verify all accepted requests have results; preserve completed runs and paused
   historical tasks. Do not change schemas, credentials or historical evidence.
2. Test native task dispatch and worker-to-evolution-loop behavior offline, plus
   rejection of retired bounded tasks so they cannot replay as native tasks.
3. Restore upstream task service and worker entrypoint. Remove local execution,
   continuation and completion-review writers. Retain read-only history reports.
4. Restore native task UI; switch the existing launcher to native mode. Replace
   bounded skill instructions with native API/config instructions and research prompts.
5. Run relevant local tests and UI build; commit and deploy via ops/local.ps1.
   Verify worker/API/UI and preserved history. Remote CI is asynchronous.

No new scheduler, worker, database migration or automatic Alpha submission.
