# Audited local PPA expansion

Continuous PPA runs enable local expansion by default. Set `ppa_local_expansion: false` in the task configuration to disable it. Finite PPA runs retain model-only preparation.

The coordinator uses saved, measured campaign candidates as refinement parents when it can form a valid batch. It changes one time-series lookback at a time, and only to a value previously used by a saved `RESULT` or `CACHED` candidate with the same operator. If no such refinement exists, it asks Cockpit for new cases. A fresh Cockpit batch may also be expanded by replacing a single field within the same dataset or changing one shared time-series lookback to a measured value. Discovery variants preserve the control plus three-treatment group and its single-factor comparison.

Every resulting case goes through the existing manifest, semantic, hypothesis, parent-budget, historical-signature, behavioral-signature, exhausted-template, and batch-size checks. The import endpoint independently replays local expansion against current feedback. It rejects a stale or modified batch, including a changed plan hash or expansion count. Result qualification and WQ request accounting are unchanged.

`candidate_generation.source` distinguishes `cockpit_local_responses`, `cockpit_seed_local_expansion`, and `measured_seed_local_expansion`. The latter records `model: none` because that batch makes no new model call; its parents are saved campaign measurements. The rule version and seed digest identify the deterministic expansion rule and input. This is a candidate-supply optimization, not evidence of higher WQ throughput or Alpha quality. Measure saved distinct WQ results over a full quota window before claiming a daily rate.
