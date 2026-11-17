# AIAC operator validation

Change: consolidate aiac-research/aiac-review into one workspace-discoverable aiac-operator.
Scope: skill instructions/references, activation metadata and offline interface tests only.
No application redeployment, schema/credential change, WQ/LLM call or research dispatch.

## Baseline review

The former skills had separate dispatch/review routing and no concrete Playwright research
workflow or interactive requirement procedure. The replacement retains native APIs,
history freezes and quality distinctions; adds explicit login handling, a bounded handoff,
next-session restoration, pagination and native RESUME/permission limitations.
It does not restore the retired execution/continuation/completion-review writers.

## Decision walkthrough (author review, not an independent model evaluation)

| Input / fixed evidence | Observed decision from applying the instructions |
|---|---|
| “帮我跑因子回测”, no region/hypothesis/limits in context | Skill applies; inspect existing facts, ask missing research requirements; no create/start yet. |
| Full research request with region, dataset, hypothesis, 10 rounds; community logged in | Skill applies; validate fields, read relevant community material, summarize settings, create/start once without another approval question; hand off. |
| “run 91 最新结果”, completed run with one FAIL and Sharpe -0.2 | Skill applies read-only; report execution completion separately from zero PASS; no new task. |
| “暂停任务 90”, one active run | Native PAUSE then read status; do not claim already submitted remote work was cancelled. |
| “Worker 一直 PENDING”, missing job consumption | Maintenance branch; check service/queue/logs/trace; do not create a research task or second Worker. |
| “解释 Sharpe”, no AIAC operational request | Near miss: ordinary explanation, no skill execution or service access needed. |
| “给另一个 quant repo 跑股票策略回测” | Near miss: use that repo's workflow, not AIAC. |
| Community navigates to sign-in, or forum connector rejects auth | Ask user to log in through browser; local trace analysis can continue; dependent dispatch waits. Never ask for cookies. |
| Create/start response times out after possible enqueue | Read task/run evidence; do not repeat POST or silently create a replacement. Unresolved uncertainty is reported. |
| Native batch rejected with 403; claimed single simulation allowance is 10 | Distinguish batch permission from slots; pause through native control when applicable, report limitation; no new executor. |
| Worker replies to ping but no task trace/job receipt within 60 seconds | Return actual IDs and “queued, execution unconfirmed”; end chat wait. Ping alone is not progress. |
| run 91 reports total 145 Alphas but CLI returns first 100 | Fetch remaining pages through native API before claiming full review; CLI does not claim an exhaustive report. |
| Two plausible tasks for “继续” | Ask which task/research; do not guess an account-consuming dispatch. |
| Old bounded task or UNKNOWN request | Read historical report only; no resume/replay or request-state rewrite. |

## Executable local checks

- `python -m unittest ops.test_aiac_operator -v`: mock native create/start/report, preserved
  research metadata, historical read-only reporting, pagination total and no retry on timeout.
- Skill Creator `quick_validate.py`: frontmatter and skill format.
- Check local Markdown links, activation metadata, API method/path correspondence against
  the locally served OpenAPI (GET only), and removal of old active entrypoints.
- Preserve backend release pointer; no backend/UI build or deploy is needed for these docs.

These checks establish interface compatibility and a reviewed instruction flow. They do
not prove that an LLM will always obey prompts, current WQ batch permission, authenticated
community access, or detached completion of a real native run. Verify those during the
next explicitly requested research task. Do not make a real simulation a hidden smoke test.

## Delivery decision

Promote to the project workspace after the listed local checks pass. The user's approved
plan authorizes this consolidation and workspace installation; no global skill installation.
The repository is the maintained source; the workspace entry points to it. Retain old source
in Git and private backup of old active files, outside skill discovery. Keep archived WQ skills untouched.

Local outcome, 2026-11-17: 7 mocked interface tests passed; Skill Creator validation,
all relative documentation links, automatic-invocation metadata and 10 method/path
contracts against the local OpenAPI passed. The active workspace entry is a directory
junction to the repository skill; both former active directories were moved to the
private runtime retired-skills backup. The backend release remains 597689f; no research started.
