# Report contract

Use the active version of `scripts/aiac_research.py --url API_URL report --run-id RUN_ID`, after checking its help and active deployment pointer. The companion `aiac-research/references/tool-contract.md` describes path resolution.

Required evidence: frozen manifest, individual request states, Alpha identities, raw metrics/checks, backend quality labels and final PPA status when available. Missing fields must be reported rather than reconstructed from aggregate counts or stale memories.

The implemented bounded report exposes observed_at, recipe_commit, requests (state counts), quality_PASS, PPA_READY, cases and alphas. Each Alpha has metrics, is_metrics, settings and checks. Read metrics.raw_wq.test for test-period observations when WQ returned them, and metrics.failures/missing for the backend's decision evidence. Check both counts and individual case lineage. PPA_READY currently remains 0/unproven; a COMPLETED run is an execution state only. Initial execution mode is frozen single-simulation cases, without automatic expansion.

Do not assume the legacy research_candidates endpoint exists in a clean deployment. Existing Run/Trace/Alpha records and the current report endpoint are the authority. Saved reports are snapshots; preserve their observation time.

Review is GET/read-only. Do not call start/resume/pause/stop, write SQL, change a report, or submit an Alpha to make an answer look complete.
