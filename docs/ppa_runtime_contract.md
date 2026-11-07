# PPA continuous runtime contract

- `theme_ppa_v3` enters `run_parallel_batch` on the normal start and resume paths.
- Each pending case has one independent audited request and result commit. Existing `RESERVED`, `REQUESTED`, and `UNKNOWN` requests must reconcile before new dispatch.
- New or uncalibrated runs start at at most 8 in flight. A burst 429 halves the saved concurrency, to a floor of 2; observed daily exhaustion waits for reset.
- The first discovery review stops after the entire first batch completes and verifies every case before resume. The continuous phase capacity is 16, with no experiment flag or transport switch.
- Every batch emits `ppa_batch` with `ppa_path=parallel`, `concurrency`, and `cases`. A run 18 acceptance check must observe `concurrency >= 4` on an unthrottled batch and overlapping in-flight requests.
- Compare completed WQ requests and active WQ wall time before and after release. A green CI run alone is not throughput evidence. Preserve old request transport metadata for recovery.
