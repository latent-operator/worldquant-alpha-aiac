"""Capture and preserve AIAC task/run identities during a local code release."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

from sqlalchemy import func, select, text

from backend.database import AsyncSessionLocal
from backend.models import Alpha, ExperimentRun, MiningTask
from backend.services.task_service import TaskService


ACTIVE = frozenset({"RUNNING", "WAITING_LLM"})


def active_entries(snapshot: dict) -> list[dict]:
    return [entry for entry in snapshot["tasks"] if entry["task_status"] in ACTIVE]


def validate_resume_target(before: dict, current: dict) -> None:
    by_id = {entry["task_id"]: entry for entry in current["tasks"]}
    for entry in active_entries(before):
        found = by_id.get(entry["task_id"])
        if not found or found["run_id"] != entry["run_id"]:
            raise ValueError("The original run ID changed during deployment")
        if found["task_status"] not in {"PAUSED", "RUNNING", "WAITING_LLM"}:
            raise ValueError("Original task is not resumable")


def validate_persistent_state(before: dict, after: dict) -> None:
    if after["alpha_count"] < before["alpha_count"]:
        raise ValueError("Saved WQ Alpha result count fell during deployment")
    current = {entry["task_id"]: entry for entry in after["tasks"]}
    for entry in before["tasks"]:
        found = current.get(entry["task_id"])
        if not found or found["run_id"] != entry["run_id"]:
            raise ValueError("Task or run ID changed during deployment")


async def capture() -> dict:
    async with AsyncSessionLocal() as db:
        tasks = (await db.execute(select(MiningTask).order_by(MiningTask.id))).scalars().all()
        runs = (await db.execute(select(ExperimentRun).order_by(ExperimentRun.id.desc()))).scalars().all()
        latest = {}
        for run in runs:
            latest.setdefault(run.task_id, run)
        alpha_count = (await db.execute(select(func.count(Alpha.id)))).scalar_one()
        revision = (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        return {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "db_revision": revision,
            "alpha_count": alpha_count,
            "run_count": len(runs),
            "tasks": [{
                "task_id": task.id,
                "task_status": task.status,
                "run_id": latest[task.id].id if task.id in latest else None,
                "run_status": latest[task.id].status if task.id in latest else None,
            } for task in tasks],
        }


async def pause_active(snapshot: dict) -> None:
    async with AsyncSessionLocal() as db:
        service = TaskService(db)
        for entry in active_entries(snapshot):
            await service.intervene_task(entry["task_id"], "PAUSE")
    current = await capture()
    validate_resume_target(snapshot, current)
    for entry in active_entries(snapshot):
        actual = next(item for item in current["tasks"] if item["task_id"] == entry["task_id"])
        if actual["task_status"] != "PAUSED" or actual["run_status"] != "PAUSED":
            raise ValueError("An active task did not reach PAUSED state")


async def resume_original(snapshot: dict) -> None:
    current = await capture()
    validate_resume_target(snapshot, current)
    by_id = {entry["task_id"]: entry for entry in current["tasks"]}
    async with AsyncSessionLocal() as db:
        service = TaskService(db)
        for entry in active_entries(snapshot):
            if by_id[entry["task_id"]]["task_status"] == "PAUSED":
                result = await service.intervene_task(entry["task_id"], "RESUME")
                if result.get("run_id") != entry["run_id"]:
                    raise ValueError("Resume did not reuse the original run ID")


async def _run(args) -> None:
    if args.action == "capture":
        print(json.dumps(await capture(), sort_keys=True))
        return
    snapshot_path = Path(args.snapshot)
    if args.action == "pause":
        if snapshot_path.exists():
            raise ValueError("Snapshot path already exists")
        snapshot = await capture()
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
        await pause_active(snapshot)
        print(json.dumps({"paused_task_ids": [e["task_id"] for e in active_entries(snapshot)]}))
        return
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if args.action == "resume":
        await resume_original(snapshot)
        print(json.dumps({"resumed_task_ids": [e["task_id"] for e in active_entries(snapshot)]}))
    elif args.action == "verify":
        current = await capture()
        validate_persistent_state(snapshot, current)
        print(json.dumps(current, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("capture", "pause", "resume", "verify"))
    parser.add_argument("--snapshot")
    args = parser.parse_args()
    if args.action != "capture" and not args.snapshot:
        parser.error("--snapshot is required")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
