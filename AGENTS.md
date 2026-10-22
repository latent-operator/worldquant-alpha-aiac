# AIAC local operations

Codex is the operator for this repository. For every code change requested in natural language, complete the software loop: inspect the current task and Git state, work on a branch, add or update offline tests, run the preflight checks, commit the change, obtain CI evidence, then deploy only when the user has requested a release. Report the commit, checks, release record, and any remaining blocker.

- Preserve the existing AIAC PostgreSQL volume, task IDs, run IDs, checkpoints, and encrypted credentials. Never use `docker compose down -v` on the live project.
- Keep the old `F:\wq-capability-loop` paused and untouched. AIAC is the only active mining runtime.
- No CI or smoke check may call a live LLM, WQ simulation, or WQ submission endpoint. Use mocks and isolated databases.
- Do not start a new mining task as part of a software release. Resume only tasks that were active before the release, using their existing run IDs. Respect prior PAUSED and STOPPED states.
- Never print, commit, copy into images, or put in release records the contents of `.env`, `keys.txt`, BRAIN credentials, LLM keys, or `CREDENTIALS_SECRET`.
- A public push is blocked until the owner confirms the historical `keys.txt` credentials are revoked or rotated. Scan every new file and change before committing.
- Deploy only a clean, named commit with successful CI on the writable fork. Record the previous commit, CI run, database revision, backup ID, and pre/post task and run states.
- API and worker must use the same `.env` and exactly one worker. A release failure restores the previous code version; never auto-restore a live database after writes may have occurred.

Run `python scripts/preflight.py` before each commit. The CI workflow runs the same offline checks.
