---
name: aiac-review
description: Review saved AIAC/WQ batch results, trace failures and quality evidence; recommend continue, change mechanism or stop. This is read-only and does not start experiments, reconcile unknown requests, deploy software or submit Alphas.
---

# AIAC review

Read `G:\安装\claude\work\aiac\LOCAL_CONTEXT.md` and the current release pointer in
`G:\安装\claude\runtime\aiac-clean\current.json`. Use its interpreter and
`<source>/ops/research.py report --run-id ID`, or the local read-only MCP tools.
Retired bounded runs retain saved review traces. Native runs expose original run,
trace and Alpha records; read native analysis/round summaries when present.
The added completion-review writer and Windows notification path are retired.
Missing optional analysis or execution completion never changes saved quality labels.

- Establish task/run, candidate signature, frozen settings, WQ Alpha ID, code commit and observation time.
- Count RESULT separately from UNKNOWN/REQUESTED/RESERVED, rejected and failed requests. Incomplete
  comparisons stay incomplete; neither HTTP success nor a completed run proves quality.
- Compare like-for-like cases using Sharpe, Fitness, AMER, turnover, margin, test-period and
  risk-neutralized results when present. Margin is return per traded value, not collateral.
  An internal test segment is not independent OS; coverage alone is not PIT evidence.
- Preserve backend quality labels and raw failed/missing checks; flag contradictions rather than
  relabeling results. The goal needs persisted PASS plus final PPA_READY for the same Alpha.
- Explain the decisive failure evidence and recommend a specified contrast, a new mechanism/data,
  or stop. Three unsuccessful family iterations require change or stop; stopping earlier is valid.
- Return a compact report with completeness, quality/PPA outcome, decisive metrics and limitations.

A recommendation does not dispatch. Only a separate instruction to continue/test hands off to
aiac-research. Never change request states, replay uncertainty or submit an Alpha during review.
