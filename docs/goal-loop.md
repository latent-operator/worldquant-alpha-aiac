# Local goal loop

The pinned [Docker tool image](../compose.goal-tools.yaml) runs OpenSpec 2.0.0 and Beads 1.3.4. It mounts this worktree at `/workspace` and the Git common directory at `/git`, where Beads keeps its local Dolt database. No WQ credentials or AIAC database are mounted. The image has no background service; each command starts a short-lived container.

From PowerShell in this worktree:

```powershell
docker compose -f compose.goal-tools.yaml build
.\scripts\goal-tools.ps1 openspec validate --all --strict
.\scripts\goal-tools.ps1 bd prime
.\scripts\goal-tools.ps1 bd ready
.\scripts\goal-tools.ps1 bd show aiac-r1e
```

`AIAC-PPA-QUALIFIED-ALPHA` is the stable goal. OpenSpec defines its evidence contract. Beads epic `aiac-r1e` has children for the old-request audit, frozen candidate batch, isolated WQ run and first-PASS review. The WQ run depends on both audit and freeze; review depends on the run. Beads data persists in the local Git common directory across worktrees and short-lived tool containers. It is **not** committed to the code branch; use Beads Dolt sync deliberately if cross-machine state is needed.

Before claiming research progress, run the read-only evidence check against a saved report:

```powershell
.venv\Scripts\python.exe -m scripts.goal_evidence <report.json> --source <run-or-batch>
```

The gate accepts both the historical comparison report and the new discovery campaign's `candidate_lineage` report. It reports `EVIDENCE_INSUFFICIENT`, `NOT_PROVEN`, `PASS_OBSERVED` or `PPA_READY_OBSERVED`. The final status requires candidate-level quality and final PPA evidence for the same WQ Alpha; preliminary `platform_ppa_status` does not count. This is an observation gate, not an automated Alpha submission decision. The historical old-lab report returns 35 results, 45 `UNKNOWN`, zero PASS and `EVIDENCE_INSUFFICIENT`.

For a new research iteration, record the economic hypothesis and frozen manifest in a Beads issue; attach task/run/candidate/Alpha identifiers and WQ feedback after execution. A failed family repeated three times requires a documented mechanism change, dataset change or stop decision. AgentsView remains a session trace, and AIAC ResearchLedger remains the business record.

## Recovery, acceptance and backup

The local workspace root `G:\安装\claude\AGENTS.md` now directs AIAC sessions in all child worktrees to `G:\安装\claude\tools\aiac-goal.ps1 resume`. That launcher prints the specification, persistent Beads memories and ready work. Deployment templates are in `tools/goal-loop/workspace-AGENTS.md` and `workspace-entry.ps1`; preserve other root instructions when reinstalling. Fresh PowerShell recovery was verified from the main and live-eval worktrees. This does not prove that every agent host automatically obeys instructions after compaction.

Report-only mode exits zero when reading a valid report, even if the goal is unproven. Use `python -m scripts.goal_evidence <report> --source <source> --require-ready` for a fail-closed acceptance check. Contradictory request counts, missing request states, duplicate candidates/Alpha IDs and wrong run references are rejected. Complete lineage and a manifest hash are required for goal acceptance. Report authenticity still relies on the existing trusted ledger export; this command cannot establish authenticity of an arbitrary supplied JSON file.

Close the research epic through:

```powershell
G:\安装\claude\tools\aiac-goal.ps1 close-goal <persisted-report.json> --receipt <private-new-receipt.json> --session-id <actual-session-id>
```

The command records report SHA256, code commit, session, manifest, run, candidate and WQ Alpha before attempting the Beads close. Failure leaves `closed=false` in the receipt. The common launcher rejects direct close/update/delete of the root epic; direct access to Docker or the Dolt database can bypass this workflow guard. Ordinary child tasks remain editable.

Run `goal-tools.ps1 bd backup sync` after substantive task updates. The configured Dolt-native backup is outside the repository at `G:\安装\claude\backups\aiac-beads`, mounted as `/backup`. Restoration was tested in a disposable container with a new Git repository and a read-only backup mount. Never run `bd backup restore --force` against the active database as a smoke test.

Pure documentation/archival changes use `python scripts/preflight.py --docs-only` and both OpenSpec `validate --all --strict` and `validate --archived --strict`. The local classifier includes untracked files and refuses code/configuration/agent-policy changes. PR CI classifies the full base-to-head diff; manual CI dispatch always runs the full suite. Keep final checklist/archival updates in the implementation change before the final validation cycle.
