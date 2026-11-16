---
name: aiac-research
description: Plan and dispatch bounded real WorldQuant BRAIN research through local AIAC when the owner asks to continue AIAC research, test a hypothesis, or create and run an experiment. Restore durable context, freeze candidates, use native background execution, and return task/run receipts. Do not use for read-only result interpretation, generic finance advice, repository maintenance, or requests to submit Alphas.
---

# AIAC research

Use AIAC's native queue and worker. The Skill guides decisions; it is not a scheduler and must not keep a chat alive polling an ordinary dispatched batch.

## Recover and scope

1. Run `G:\安装\claude\tools\aiac-goal.ps1 resume`. If recovery fails, report it; do not reconstruct authorization.
2. Read the active deployment pointer and latest persisted reports. Resolve paths and versions from those records rather than remembering a previous checkout.
3. Apply the owner's standing default authorization: when asked to define or adjust an AIAC research task, complete validation and dispatch real WQ backtests without asking again whether to start. This excludes Alpha submission, legacy resume/replay and uncertain POST retransmission.
4. A question about usage or a read-only review does not authorize a new experiment. Read `../aiac-review/SKILL.md` for review-only work.

## Form one bounded experiment

Read prior trace evidence before proposing another family. Distinguish community hypotheses from platform rules. Freeze the economic mechanism, real field IDs, settings, exact candidates, request/candidate budgets, and stop criteria in the backend's accepted manifest. Keep comparable settings across contrasts.

Verify expression syntax, operator/field limits, denominator treatment and historical exclusions. Record missing PIT, accounting or coverage evidence honestly. Diagnostic simulations may collect missing evidence; they cannot establish quality PASS through assumptions. Repeated failure should lead to a changed mechanism or stop decision, not arbitrary sign reversal or silent threshold relaxation.

## Dispatch and return

1. Read [the tool contract](references/tool-contract.md), inspect the installed CLI help, and run health. Verify the current worker is ready and the backend enforces the frozen budgets and request protection.
2. Create the task once, retain its returned identity, then start once. A timeout is not evidence that creation/start failed: reconcile by reading task/run state before any repeated action.
3. Return the actual task ID, run ID, accepted budget and UI/report location. Say queued/running only when observed. Ordinary dispatch ends here: no Playwright polling loop and no fabricated completion notification.
4. If the user explicitly asks to watch the run through completion, follow through in that task with bounded read-only status checks.
5. On a later request, use persisted reports and `aiac-review` before forming the next batch.

If worker readiness or enforcement is absent, report the concrete blocker and retain the prepared manifest. Do not silently bypass it with browser POSTs, raw SQL changes, an alternate worker or a new orchestration framework.

## Request boundaries

Backend facts are authoritative. Preserve UNKNOWN/REQUESTED/RESERVED positions. A known accepted Location permits GET polling recovery; it does not permit a replacement POST. A frozen legacy signature remains excluded. Pause on 429 or uncertain request outcomes according to the backend contract. Secrets stay in the existing private configuration.

Do not claim a business improvement from CI, HTTP success, worker health or request counts. Goal acceptance needs persisted quality PASS and final PPA_READY for the same traceable Alpha; submission is separate.
