# Local AIAC development

Owner policy, 2026-11-16: relevant local verification is sufficient for delivery.
Remote CI runs asynchronously and does not block local deployment. This supersedes
the old full-preflight, master-push-CI, Beads and OpenSpec prerequisites.

- Read `LOCAL_CONTEXT.md` and the private `runtime/aiac-clean/current.json` pointer.
- Work on a branch. Preserve other uncommitted work. Commit reviewed changes before deployment.
- Run tests for the changed behavior. Build the UI when changing it. Do not run unrelated legacy suites.
- Use `ops/local.ps1` for local Start, Stop, Status, Update and Rollback. No second deployment framework.
- Code-only releases preserve the database and record the old/new code versions. Never restore a database automatically on code rollback.
- Schema, data or credential changes require a fresh private backup and restore validation before mutation. The code-only Update command is not a migration runner.
- Keep completed-batch/zero-pending boundaries when switching running research code. Never rewrite request state to pass a gate.
- Preserve legacy task12/task14/run28, UNKNOWN requests and frozen-expression exclusions. Cleanup does not reconcile or replay research.
- Bounded execution uses the original worker with a frozen manifest, single requests, fixed budget and durable receipts. Enable it only through the release pointer; beat remains off. Never use browser POSTs to bypass execution guards.
- No live WQ/LLM calls in tests, CI or smoke checks. No Alpha submission without explicit instruction.
- The owner authorizes real backtests by default when defining a research task through the verified bounded executor. A review or maintenance request is not a new experiment.
- PASS requires persisted quality evidence; the research goal additionally requires final PPA_READY for the same Alpha. HTTP success, CI or deployment is not goal acceptance.
- Credentials and raw research receipts remain private. Never commit `.env`, account details, tokens or database dumps.

Documentation and skill-only edits need format/link or focused workflow checks, not application deployment.
Do not introduce mandatory governance tools, extra task ledgers or resident agent polling.
