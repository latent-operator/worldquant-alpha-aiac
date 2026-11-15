# Local AIAC research continuity

Standing owner authorization (2026-11-15): after successful CI, perform local production CD by default without requesting release approval again. Integrate through the writable fork, obtain successful master-push CI for the exact commit, and use the audited release procedure. Preserve completed-batch and zero-pending-request gates, backup/restore validation, rollback source and release journal. A separately approved ONE-TIME exception for task 14 / run 28 permits audited incomplete-batch isolation: correct ambiguous FAILED to UNKNOWN with provenance, preserve positions and cursor, block frozen-signature replay and old-run resume, and retain research hold. This is software CD only, not reconciliation or automatic research restart. Never falsify request or batch state to force deployment.

For AIAC/WQ research or operation tasks under this workspace, start or recover a session by running `G:\安装\claude\tools\aiac-goal.ps1 resume`. This loads the durable OpenSpec goal, Beads memories and currently ready work from the installed tooling worktree. Follow it even when working in a different AIAC worktree. If recovery fails, report the failure instead of reconstructing permissions from assumptions.

Use the same launcher with `close-goal <persisted-report> --receipt <private-path> --session-id <actual-session-id>` to accept the research goal. CI success or completion of a deployment task does not close the research goal. Preserve existing task-control authorizations and production release boundaries.

For pure documentation/archival changes, the tooling worktree supports `python scripts/preflight.py --docs-only` and OpenSpec strict validation. Code, configuration and agent-policy changes retain full preflight. This narrow documentation exception supersedes an inherited blanket "every commit" rule; it does not weaken runtime or release verification.

These instructions apply to AIAC-related work only.
