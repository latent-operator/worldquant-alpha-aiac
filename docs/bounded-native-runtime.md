# Bounded native research

The clean installation uses pinned upstream `8ffda431d08703b7f6ae13c8287806fd76eb63e0` plus the small versioned integration in `scripts/build_clean_runtime.py` and `clean_runtime/bounded.py`. It preserves the original API, Celery queue, task/run/trace/Alpha tables and UI. The old fork application's research framework is not copied into the clean runtime.

The active release pointer supplies `recipe_source`, generated application `source`, `python_executable`, ports and exclusion-manifest hash. The generated source and UI are hashed in `bounded-build.json` and checked at service startup. The recipe checkout is a clean exact CI commit. Do not edit generated source or the private configuration.

## Dispatch

Use `python_executable` with `recipe_source/scripts/aiac_research.py`. Commands are `health`, `create --manifest FILE`, `start --task-id ID`, `resume --task-id ID`, `report --run-id ID`, `pause --task-id ID` and `stop --task-id ID`; `--url` defaults to local API port8003. The manifest is the existing task-create body. See `skills/aiac-research/references/tool-contract.md` for the schema.

Starting a bounded task freezes its exact candidates and settings and commits every trace position before publishing the Celery message. The caller receives task/run IDs immediately. The native single-concurrency worker owns authentication, the single-simulation POST, waiting and result persistence. No resident chat, browser or LLM is required to wait for a frozen batch. This release intentionally does not generate further candidates after its configured batch.

Candidate fields must be checked against live WQ metadata by the research workflow. The backend validates declared field count and syntax, not undocumented fiscal/PIT semantics. All original API/UI start paths route through bounded validation; generic or legacy tasks cannot bypass it.

## Request recovery

The account advisory lock prevents concurrent execution and duplicate queue deliveries from creating another simulation. The planning transaction excludes historical expressions and previously frozen signatures. Before sending a POST the trace becomes REQUESTED; a transport exception, ambiguous status or missing Location does not trigger a retry or another transport fallback. A reliable Location becomes ACCEPTED. Accepted polling may resume with GET only. RESULT commits the returned Alpha and trace together.

On worker startup, only RUNNING bounded runs are requeued. A REQUESTED position with no durable Location becomes UNKNOWN and pauses. PAUSED, STOPPED and legacy runs are not automatically resumed. Explicit resume retains the same run and refuses UNKNOWN, REQUESTED, REJECTED or failed positions. A known Location survives polling/authentication delays. These are local replay protections, not a claim of remote exactly-once delivery.

Health distinguishes an idle worker ping from a recent busy-worker heartbeat. A heartbeat is a bounded-age liveness observation, not proof that a result exists. The UI and report show consumed requests, unresolved positions, stored quality and execution state separately.

## Quality and review

Deterministic checks retain the existing strict numeric thresholds; missing/nonfinite evidence is not PASS. Raw WQ responses, test metrics and local failed/missing requirements remain stored with the Alpha. PPA readiness is currently UNPROVEN/0 in the bounded report; a successful worker or completed batch cannot close the research goal. No Alpha submission endpoint is called.

The two repository-local skills provide research dispatch and read-only review. Offline skill transcripts are anonymized examples and include synthetic edge cases, not business acceptance receipts. Real release/backtest evidence stays in the private runtime journal. No global skill installation is required.

## Release and rollback

Run full preflight, pinned-upstream/native database tests and frontend build. Integrate through the writable fork and require successful master-push CI for the exact recipe commit. Execute `scripts/deploy-bounded-clean.ps1` from an immutable recipe checkout with `-RuntimeHome`, `-Commit`, `-OfflineCI` and `-NativeCI`.

The release requires a completed active batch, no unresolved active requests and an empty queue. It stops the old services, creates and restore-tests a fresh active-database dump, preserves the previous pointer, starts one bounded worker plus API/UI, then updates the existing Ensure scheduled task. On failure it restores the prior code pointer without automatically restoring a database after possible writes. Frozen historical containers, volumes and archives remain untouched.
