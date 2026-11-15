# Local AIAC research continuity

For AIAC/WQ research or operation tasks under this workspace, start or recover a session by running `G:\安装\claude\tools\aiac-goal.ps1 resume`. This loads the durable OpenSpec goal, Beads memories and currently ready work from the installed tooling worktree. Follow it even when working in a different AIAC worktree. If recovery fails, report the failure instead of reconstructing permissions from assumptions.

Use the same launcher with `close-goal <persisted-report> --receipt <private-path> --session-id <actual-session-id>` to accept the research goal. CI success or completion of a deployment task does not close the research goal. Preserve existing task-control authorizations and production release boundaries.

For pure documentation/archival changes, the tooling worktree supports `python scripts/preflight.py --docs-only` and OpenSpec strict validation. Code, configuration and agent-policy changes retain full preflight. This narrow documentation exception supersedes an inherited blanket "every commit" rule; it does not weaken runtime or release verification.

These instructions apply to AIAC-related work only.
