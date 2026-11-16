# Completion review implementation plan

**Goal:** bounded batch completion automatically produces a persisted review and AIAC/Windows notifications.
**Architecture:** reuse the native Celery worker, existing LLM service and TraceStep JSON. No new service, table, scheduler or chat loop. Authoritative quality remains backend facts; review cannot dispatch research.

1. Add isolated database tests for completed-only review, duplicate delivery, failure fallback and restart recovery. No live WQ/LLM in tests.
2. Add a completion handler and one native recovery task. Store one REVIEW trace per run under the existing run row lock; recover missing completed reviews on worker startup. Desktop delivery is attempted once, with its result recorded separately.
3. Replace the static header badge with persisted completion notifications; show facts and advisory review on the existing task page. Use native Windows toast from the same worker.
4. Run bounded tests and UI build, commit, deliver with ops/local.ps1. Use already completed run2 to verify native recovery/review/notifications without new simulations. Push asynchronously.

Limits: no automatic next experiment, Alpha submission, final PPA evaluation or automatic Codex chat wakeup. Desktop notifications depend on Windows notification settings; the in-app record remains durable.
