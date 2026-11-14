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
