# news17 paper proxy experiment

`paper_news17_ablation_v1` is a bounded AIAC worker protocol for arXiv:2512.19484. Its fields are **vendor news17 proxies**, not the paper's LLM and attention-model features. It does not import a third-party Alpha or performance result.

The protocol runs a fixed 5-window matrix. Each window shares one event-sentiment control across four treatments (event relevance, entity relevance, similarity days, and event density), so the matrix has at most 25 distinct WQ candidate requests. Afterward it selects only treatments whose Sharpe and Fitness both improve over the matched control and whose Sharpe, Fitness, turnover and margin are present. Constrained operator/window mutations from those seeds consume at most 15 more requests. A better genetic child may seed the next mutation batch. If no eligible seeds or novel valid variants remain, the task completes with unused budget.

Task configuration requirements:

```json
{
  "research_protocol": "paper_news17_ablation_v1",
  "research_request_budget": 40,
  "research_only_fields": [
    "nws17_d1_event_sentiment_score", "nws17_d1_event_relevance",
    "nws17_d1_event_similarity_days", "nws17_d1_rp_story_event_count",
    "nws17_d1_qmb", "nws17_d1_ssc", "nws17_d1_qep", "nws17_d1_ber",
    "nws17_d1_acb", "nws17_d1_relevance", "nws17_d1_rp_story_event_index"
  ],
  "delay": 1,
  "decay": 4,
  "neutralization": "SUBINDUSTRY",
  "truncation": 0.08,
  "test_period": "P2Y0M",
  "max_operator_count": 5,
  "paper_reference": "https://arxiv.org/abs/2512.19484"
}
```

The task must select only `news17` with region `USA`, universe `TOP3000`, and `dataset_strategy=SPECIFIC`. A mismatch pauses the run before a WQ request. The request ledger reserves each expression and settings pair before network I/O; a timeout or incomplete response consumes that slot and is marked `UNKNOWN` for reconciliation. It is never automatically resent. Each worker dispatch saves a checkpoint and releases the worker. Pause, stop, and restart retain the original run.

Read `GET /api/v1/runs/{run_id}/research-candidates?limit=50&offset=0` for lineage and `GET /api/v1/runs/{run_id}/research-report` for funnel, budget, paired deltas, strict quality status and gate failures. `PASS` means the persisted Alpha passed complete gates. `OPTIMIZE` remains a failed quality result. Older runs have `audit_available=false` and retain their original Alpha rows and trace.
