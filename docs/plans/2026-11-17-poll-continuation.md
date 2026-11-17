# Poll continuation repair

Root cause: bounded_research.run shares a 1,800-second polling budget across the whole batch and converts exhaustion to PAUSED; the native task only chains COMPLETED runs. Later candidates can consume little polling time before this batch clock expires. Resume also returns no action to the existing intervention router.

1. Recover the saved final Location using the existing native resume path; hold between completed batches for code delivery. Preserve all request states and POST count.
2. Test budget exhaustion -> native queue retry of the same run, GET-only recovery for accepted requests, owner pause precedence, and resume response contract.
3. Replace timeout pause with a durable RUNNING/yield receipt and existing Celery retry (no retry-count cap). Keep auth, rate-limit and UNKNOWN holds.
4. Test automatic mechanism replacement: fully failed completed families and review STOP are excluded; allow up to three local planner corrections without sending simulations for invalid candidates.
5. Run local focused tests, deploy through ops/local.ps1, resume the same continuous task and verify a new mechanism is accepted. Remote CI asynchronous. No new service, schema or scheduler.
