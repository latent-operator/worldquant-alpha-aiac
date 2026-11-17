# Native task request

Use verified dataset/field IDs and save the actual request privately. No credentials here.

```json
{
  "name": "Native research: specified economic hypothesis",
  "region": "GLB",
  "universe": "TOP3000",
  "dataset_strategy": "SPECIFIC",
  "target_datasets": ["pricevolume6"],
  "agent_mode": "AUTONOMOUS",
  "daily_goal": 1,
  "max_iterations": 10,
  "config": {
    "delay": 1,
    "simulation_batch_size": 4,
    "target_hypothesis": "Replace with a motivated hypothesis and contrasts",
    "constraints": [
      "Judge by saved real WQ results, not execution success.",
      "Change economic mechanism after repeated failures; avoid post-hoc sign flips.",
      "Exclude failed low-volatility and intermediate-momentum mechanisms."
    ],
    "preferred_fields": ["close", "volume"],
    "avoid_operators": ["trade_when"],
    "attempted_expressions": []
  }
}
```

Populate attempted_expressions from history before dispatch. The original worker and
adapter govern execution; these fields do not add a request ledger. max_iterations
is finite. Multi-simulation permission must be established by real authorized research,
not by offline tests or an assumption about account slots.
