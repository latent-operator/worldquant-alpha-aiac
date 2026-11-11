# PPA continuous runtime contract

- `theme_ppa_v3` enters `run_parallel_batch` on the normal start and resume paths.
- Each pending case has one independent audited request and result commit. Existing `RESERVED`, `REQUESTED`, and `UNKNOWN` requests must reconcile before new dispatch.
- New runs start at 2 in flight, with a ceiling of 4. Two complete, clean, fully supplied and overlapping batches at 2 permit an increase to 4. A 429 or unresolved request reduces concurrency to a floor of 2; observed daily exhaustion waits for reset.
- The first discovery review stops after the entire first batch completes and verifies every case before resume. The continuous phase capacity is 16, with no experiment flag or transport switch.
- Every batch emits `ppa_batch` with `ppa_path=parallel`, `concurrency`, and `cases`. Batch metrics are persisted before the first-batch review pause. An acceptance check must observe actual overlapping in-flight requests before any throughput claim.
- Compare completed WQ requests and active WQ wall time before and after release. A green CI run alone is not throughput evidence. Preserve old request transport metadata for recovery.
- The coordinator is opt-in through `runtime/aiac-ops/ppa-coordinator.json`, bound to task 14, one run ID and one released commit. The manager skips it during maintenance; the lease prevents duplicate coordinators. A superseded run or user pause must never be resumed automatically.
- The activation JSON has `enabled`, `task_id`, `run_id`, `commit`, `cockpit_script`, and `output_dir`; paths must be absolute, the script must exist, and the commit must match the released version. Create or replace it only after the new run is safely created; the periodic manager restarts the matching coordinator.
