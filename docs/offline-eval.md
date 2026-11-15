# Offline AIAC eval

This development and CI layer uses DeepEval 4.3.4 custom metrics without a model judge. Install both requirement files into the same Python 3.11 environment:

```powershell
python -m pip install -r requirements.txt -r requirements-eval.txt
python scripts/preflight.py
```

The eval command in preflight sets `DEEPEVAL_DISABLE_DOTENV=1` and clears model, Confident AI, and BRAIN credentials. It uses fixed JSON and local functions; it makes no LLM, WQ simulation, or submission request. Do not log in to Confident AI for this workflow.

## Evidence and decisions

`backend/tests/evals/fixtures/ppa_runs_v1.json` is a versioned allowlist snapshot of read-only AIAC research reports and paginated candidate lists for task 14, runs 18 and 25–28. It contains candidate IDs, request/quality statuses, batch case labels, counts, and timing aggregates. Candidate ID is the persisted request reservation key; AIAC has no independent request ID. The historical source commit, AgentsView session ID, and batch cursor are null because these report endpoints cannot prove them. No expression, settings, credentials, raw tool output, or session transcript is saved.

The lifecycle metric calls `validate_boundary`, the same batch completion and request reconciliation logic used by the AIAC handoff. Synthetic counterexamples cover incomplete cursor, `RESERVED`, `REQUESTED`, `UNKNOWN`, HTTP 429, and stale run ID. The historical run 28 snapshot contains two `UNKNOWN` candidates and cannot pass a safe boundary. Historical snapshots do not contain the full checkpoint, so no earlier run is certified as a release boundary by this fixture alone.

The quality metric checks the persisted Alpha `quality_counts` against saved result count and the reported PASS count. Best Sharpe is context only; run 18 reaches 1.67 while its 212 saved results have zero quality PASS. AgentsView session completion never contributes to PASS.

The throughput metric accepts a faster claim only for two explicitly comparable windows with completed requests, full timing coverage (`timed_requests + recovered_requests = completed_requests`), interval-union WQ wall time, observed parent peak, zero pending requests, and pause time split by reason. A missing window, unknown request, missing overlap observation, or different comparison key requires the claim `INSUFFICIENT_EVIDENCE`. The historical report's WQ wall aggregate may span a campaign while its result count is run scoped. The snapshot labels this mixed scope explicitly; it must not be used as a before/after rate without matched windows.

## Read-only local comparison

With the existing local API running on port 8003:

```powershell
python -m scripts.eval_snapshot --run-id 28
```

The command only sends GET requests to a loopback AIAC API, pages the candidate list, checks row counts and duplicate IDs, writes an allowlisted snapshot under the system temporary directory, and prints differences from the saved fixture. The observed timestamp is excluded from differences. The active pause timer may legitimately increase between snapshots. The command never calls task controls or writes AIAC state. Pass `--base-url http://127.0.0.1:PORT/api/v1` if your local API uses another loopback port.

This stage does not change the production AIAC version or task 14. A production release gate needs the full current checkpoint, request reconciliation, and the existing release record procedure.
