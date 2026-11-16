---
name: aiac-review
description: Review persisted AIAC or WorldQuant BRAIN experiment results, compare frozen candidates, explain failed checks, or decide whether to continue, change mechanism or stop. Use for batch/trace/PPA reviews and requests to assess an Alpha's actual research outcome. This is read-only; it does not dispatch backtests, reconcile unknown requests, release software or submit Alphas.
---

# AIAC review

Review evidence through existing AIAC read tools or the released report CLI described in [the report contract](references/tool-contract.md). Do not alter requests, quality labels or task state.

1. Run `G:\安装\claude\tools\aiac-goal.ps1 resume` and read the relevant task/run report, frozen manifest and raw WQ outcomes. Failure to recover context is a reportable blocker.
2. Establish lineage: task/run, candidate identity, expression/settings signature, WQ Alpha ID, source commit when known, and report observation time.
3. Count RESULTS, rejected/failed requests and unresolved UNKNOWN/REQUESTED/RESERVED separately. An incomplete comparison stays incomplete even if one returned Alpha looks strong.
4. Compare only like-for-like cases. Report Sharpe, Fitness, AMER, turnover, margin, test-period and risk-neutralized observations when present. WQ margin describes return per traded value, not collateral or margin-account requirements; retain the original metric name if unsure. Preserve platform checks and local extra thresholds separately. Do not substitute training AMER for full IS AMER, or call an internal test segment an independent OS result.
5. Treat backend numeric/quality gates as authoritative, while flagging contradictions between labels and raw evidence. Do not relabel an Alpha yourself. Missing correlation, coverage or PPA evidence remains missing.
6. Explain the principal failure mechanism and choose continue with a specified contrast, change mechanism/data, or stop. Three unsuccessful iterations of a family require a documented change or stop; earlier stopping is valid. Avoid post-hoc sign flips and moving thresholds to manufacture success.
7. Return a compact evidence summary: batch completeness, actual quality/PPA outcome, decisive metrics, limitations and proposed next action.

HTTP201, successful TraceStep, completed Run and passing CI describe execution, not Alpha quality. A goal observation requires persisted PASS plus final PPA_READY for the same candidate; Alpha submission remains a separate action. Even a qualifying result in a synthetic fixture is not a real goal acceptance receipt.

Research continuation recommendations do not start a new task. If the owner explicitly asks for a new experiment, hand off to `aiac-research`; otherwise remain read-only. Unknown request reconciliation, database mutation and release actions are outside this Skill.
