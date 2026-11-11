"""Inspect or perform a CI-gated, task-14 PPA release handoff."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


def require_current_release(record: dict, commit: str, ci_run: int) -> None:
    if record.get("commit") != commit or record.get("ci_run") != ci_run:
        raise ValueError("PPA_RELEASE_MISMATCH")


def require_maintenance(current_file: Path, maintenance_file: Path) -> None:
    if (maintenance_file.resolve() != (current_file.parent / "maintenance.lock").resolve()
            or not maintenance_file.is_file()):
        raise ValueError("PPA_MAINTENANCE_REQUIRED")


async def execute(action, run_id, manifest=None):
    from backend.database import AsyncSessionLocal
    from backend.services.ppa_handoff import inspect_ppa_boundary, close_ppa_run, install_ppa_config
    async with AsyncSessionLocal() as db:
        if action == "check":
            return await inspect_ppa_boundary(db, 14, run_id)
        if action == "close":
            return await close_ppa_run(db, 14, run_id)
        return await install_ppa_config(db, 14, run_id, manifest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "close", "install"))
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--commit")
    parser.add_argument("--ci-run", type=int)
    parser.add_argument("--current-file", type=Path)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--maintenance-file", type=Path)
    args = parser.parse_args(argv)
    if args.run_id < 1 or not args.env_file.is_absolute():
        parser.error("A positive run ID and absolute environment path are required")
    manifest = None
    journal = None
    if args.action != "check":
        if (not args.commit or not args.ci_run or not args.current_file or not args.current_file.is_absolute()
                or not args.journal or not args.journal.is_absolute() or args.journal.exists()):
            parser.error("A commit, CI run, current release and new absolute journal path are required")
        from scripts.verify_release import verify
        evidence = verify(args.commit, args.ci_run)
        current = json.loads(args.current_file.read_text(encoding="utf-8-sig"))
        if args.action == "close":
            if not args.maintenance_file or not args.maintenance_file.is_absolute():
                parser.error("Close requires the absolute release maintenance lock path")
            require_maintenance(args.current_file, args.maintenance_file)
        if args.action == "install":
            require_current_release(current, args.commit, args.ci_run)
            if not args.manifest or not args.manifest.is_absolute():
                parser.error("Install requires an absolute manifest path")
            payload = args.manifest.read_bytes()
            manifest = json.loads(payload.decode("utf-8-sig"))
            manifest_hash = hashlib.sha256(payload).hexdigest()
        else:
            manifest_hash = None
        journal = {"action": args.action, "task_id": 14, "run_id": args.run_id,
                   "candidate_commit": args.commit, "ci_run": args.ci_run,
                   "previous_release": current.get("commit"), "ci_url": evidence.get("ci_url"),
                   "manifest_sha256": manifest_hash, "outcome": "PREPARED",
                   "at": datetime.now(timezone.utc).isoformat()}
        with args.journal.open("x", encoding="utf-8") as output:
            json.dump(journal, output, indent=2)
    from scripts.launch_runtime import load_runtime_env
    load_runtime_env(args.env_file)
    result = asyncio.run(execute(args.action, args.run_id, manifest))
    if journal is not None:
        journal.update(outcome="SUCCESS", result=result, completed_at=datetime.now(timezone.utc).isoformat())
        args.journal.write_text(json.dumps(journal, indent=2), encoding="utf-8")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
