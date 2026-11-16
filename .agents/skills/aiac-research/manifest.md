# Native task manifest

Resolve interpreter and source from the current release pointer. Commands:
`health`, `create --manifest FILE`, `start|pause|stop|resume --task-id ID`, `report --run-id ID`.
The CLI calls the original local API; it does not hold a polling session.

```json
{
  "name": "Hypothesis and contrast",
  "region": "GLB", "universe": "TOP3000",
  "dataset_strategy": "SPECIFIC", "target_datasets": ["VERIFIED_DATASET"],
  "agent_mode": "AUTONOMOUS", "max_iterations": 1, "daily_goal": 1,
  "config": {
    "execution_mode": "bounded", "simulation_mode": "single",
    "hypothesis": "Verified inputs, economic mechanism and expected direction",
    "fields": ["VERIFIED_FIELD"], "max_create_requests": 1,
    "neutralization": "SUBINDUSTRY", "truncation": 0.08,
    "candidates": [{"id": "A", "expression": "VALIDATED_EXPRESSION", "decay": 4}]
  }
}
```

Replace placeholders; this is not executable research evidence. Freeze 1..16 candidates within
the agreed budget, with one or two verified fields. D1, single serial requests and a two-year
test segment are fixed by this implementation. It does not automatically expand candidates.
Health combines worker ping and recent heartbeat; a missed ping while busy is not proof of failure.
The report includes code_commit, manifest_sha256, consumed requests, explicit request states,
raw WQ metrics, local failures/missing evidence and quality_PASS. PPA_READY currently remains 0/unproven.
