"""Accept supplied persisted evidence and record an auditable Beads goal closure."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

from backend.evals.goal_evidence import require_goal_ready

ROOT = Path(__file__).resolve().parents[1]
ISSUE = "aiac-r1e"


def close_goal(report_path: Path, receipt_path: Path, session_id: str) -> dict:
    raw = report_path.read_bytes()
    evidence = require_goal_ready(json.loads(raw), source=str(report_path.resolve()))
    if not session_id.strip():
        raise ValueError("A session reference is required")
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, check=True,
                              capture_output=True, text=True).stdout.strip()
    if git("status", "--porcelain"):
        raise ValueError("Commit the acceptance implementation before closing the goal")
    commit = git("rev-parse", "HEAD")
    common = Path(git("rev-parse", "--git-common-dir"))
    if not common.is_absolute(): common = ROOT / common
    common = common.resolve()
    directory = Path(git("rev-parse", "--absolute-git-dir")).resolve()
    env = dict(os.environ, AIAC_GIT_COMMON_DIR=str(common),
               AIAC_CONTAINER_GIT_DIR="/git" if common == directory
               else "/git/worktrees/" + directory.name)
    receipt = {"issue_id": ISSUE, "session_id": session_id, "code_commit": commit,
               "report_sha256": hashlib.sha256(raw).hexdigest(), "evidence": evidence,
               "closed": False}
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite an earlier acceptance attempt.
    with receipt_path.open("x", encoding="utf-8") as file:
        json.dump(receipt, file, indent=2)
    command = ["docker", "compose", "-f", "compose.goal-tools.yaml", "run", "--rm",
               "goal-tools", "bd"]
    subprocess.run(command + ["comments", "add", ISSUE, json.dumps(receipt)],
                   cwd=ROOT, env=env, check=True)
    subprocess.run(command + ["close", ISSUE, "--reason", "Validated persisted PPA evidence: "
                              + receipt["report_sha256"]], cwd=ROOT, env=env, check=True)
    receipt["closed"] = True
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args()
    try:
        result = close_goal(args.report, args.receipt, args.session_id)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"closed": False, "error": str(exc)}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
