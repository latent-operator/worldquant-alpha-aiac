"""Transactional PPA handoff at a verified, fully reconciled batch boundary."""
from datetime import datetime, timezone
from copy import deepcopy
from types import SimpleNamespace

from sqlalchemy import select

from backend.models import ExperimentRun, MiningTask, ResearchCandidate
from backend.services.ppa_progress import close_pause
from backend.services.theme_ppa_v2 import needs_reconciliation, new_fields_have_evidence


def validate_boundary(checkpoint, rows):
    cases = (checkpoint.get("ppa_batch") or {}).get("cases") or []
    ids = [case.get("case_id") for case in cases if isinstance(case, dict)]
    if (not cases or len(ids) != len(cases) or not all(ids) or len(set(ids)) != len(ids)
            or checkpoint.get("ppa_batch_cursor") != len(cases)):
        raise ValueError("PPA_HANDOFF_BATCH_INCOMPLETE")
    if any(needs_reconciliation(row) for row in rows):
        raise ValueError("PPA_HANDOFF_REQUEST_PENDING")
    completed = {row.experiment_group for row in rows
                 if (row.request_status in {"RESULT", "CACHED"}
                     and (row.result_snapshot or {}).get("ppa_evaluation"))
                 or (row.request_status == "FAILED" and
                     isinstance((row.result_snapshot or {}).get("http_status"), int) and
                     400 <= row.result_snapshot["http_status"] < 500 and
                     row.result_snapshot["http_status"] not in {408, 429})}
    if not set(ids).issubset(completed):
        raise ValueError("PPA_HANDOFF_BATCH_INCOMPLETE")


async def _locked_paused_boundary(db, task_id: int, expected_run_id: int):
    task = (await db.execute(select(MiningTask).where(MiningTask.id == task_id)
                             .with_for_update())).scalar_one_or_none()
    if not task or (task.config or {}).get("research_protocol") != "theme_ppa_v3":
        raise ValueError("PPA_HANDOFF_TASK_INVALID")
    run = (await db.execute(select(ExperimentRun).where(ExperimentRun.task_id == task_id)
                            .order_by(ExperimentRun.id.desc()).limit(1)
                            .with_for_update())).scalar_one_or_none()
    if (not run or run.id != expected_run_id or task.status != "PAUSED" or run.status != "PAUSED"
            or run.block_reason not in {"PPA_FIRST_BATCH_REVIEW", "AWAITING_RESEARCH_BATCH"}):
        raise ValueError("PPA_HANDOFF_RUN_STATE_INVALID")
    checkpoint = run.checkpoint or {}
    rows = (await db.execute(select(ResearchCandidate).where(ResearchCandidate.run_id == run.id)
                             .with_for_update())).scalars().all()
    validate_boundary(checkpoint, rows)
    return task, run, checkpoint


async def inspect_ppa_boundary(db, task_id: int, expected_run_id: int) -> dict:
    task, run, checkpoint = await _locked_paused_boundary(db, task_id, expected_run_id)
    return {"task_id": task.id, "run_id": run.id, "status": run.status,
            "cases": len(checkpoint["ppa_batch"]["cases"])}


async def inspect_empty_successor(db, task_id: int, expected_run_id: int) -> dict:
    """Allow a release between a closed complete batch and its untouched successor."""
    task = (await db.execute(select(MiningTask).where(MiningTask.id == task_id)
                             .with_for_update())).scalar_one_or_none()
    runs = (await db.execute(select(ExperimentRun).where(ExperimentRun.task_id == task_id)
                             .order_by(ExperimentRun.id.desc()).limit(2)
                             .with_for_update())).scalars().all()
    if (not task or (task.config or {}).get("research_protocol") != "theme_ppa_v3"
            or len(runs) != 2 or runs[0].id != expected_run_id or task.status != "PAUSED"
            or runs[0].status != "PAUSED" or runs[0].block_reason != "AWAITING_RESEARCH_BATCH"
            or runs[1].status != "STOPPED" or runs[1].block_reason != "PPA_HANDOFF_COMPLETED"):
        raise ValueError("PPA_HANDOFF_RUN_STATE_INVALID")
    run, previous = runs
    checkpoint = run.checkpoint or {}
    if checkpoint.get("ppa_batch") or checkpoint.get("ppa_batch_history") or checkpoint.get("ppa_batch_cursor", 0):
        raise ValueError("PPA_HANDOFF_BATCH_INCOMPLETE")
    rows = (await db.execute(select(ResearchCandidate).where(
        ResearchCandidate.run_id.in_([run.id, previous.id])).with_for_update())).scalars().all()
    if any(row.run_id == run.id for row in rows):
        raise ValueError("PPA_HANDOFF_REQUEST_PENDING")
    validate_boundary(previous.checkpoint or {}, [row for row in rows if row.run_id == previous.id])
    return {"task_id": task.id, "run_id": run.id, "previous_run_id": previous.id,
            "status": "PAUSED", "cases": 0}


async def close_ppa_run(db, task_id: int, expected_run_id: int) -> dict:
    task, run, checkpoint = await _locked_paused_boundary(db, task_id, expected_run_id)
    now = datetime.now(timezone.utc)
    run.checkpoint = {**close_pause(checkpoint, now), "ppa_handoff_at": now.isoformat(),
                      "ppa_handoff_previous_reason": run.block_reason}
    task.status = run.status = "STOPPED"
    run.block_reason = "PPA_HANDOFF_COMPLETED"
    run.next_retry_at = None
    run.finished_at = now.replace(tzinfo=None)
    await db.commit()
    return {"task_id": task_id, "run_id": run.id, "status": "STOPPED"}


async def install_ppa_config(db, task_id: int, expected_run_id: int, manifest: dict) -> dict:
    """Install a validated next-run manifest only after the previous run is closed."""
    task = (await db.execute(select(MiningTask).where(MiningTask.id == task_id)
                             .with_for_update())).scalar_one_or_none()
    run = (await db.execute(select(ExperimentRun).where(ExperimentRun.task_id == task_id)
                            .order_by(ExperimentRun.id.desc()).limit(1)
                            .with_for_update())).scalar_one_or_none()
    if (not task or not run or run.id != expected_run_id or task.status != "STOPPED"
            or run.status != "STOPPED" or run.block_reason != "PPA_HANDOFF_COMPLETED"
            or (task.config or {}).get("research_protocol") != "theme_ppa_v3"):
        raise ValueError("PPA_HANDOFF_RUN_STATE_INVALID")
    rows = (await db.execute(select(ResearchCandidate).where(ResearchCandidate.run_id == run.id)
                             .with_for_update())).scalars().all()
    validate_boundary(run.checkpoint or {}, rows)
    if not isinstance(manifest, dict) or manifest.get("theme_id") != (task.config.get("ppa_manifest") or {}).get("theme_id"):
        raise ValueError("PPA_HANDOFF_THEME_MISMATCH")
    proposed = deepcopy(manifest)
    datasets = proposed.get("datasets") or {}
    allocation = proposed.get("discovery_allocation")
    if (not isinstance(datasets, dict) or not isinstance(allocation, dict) or not allocation
            or any(name not in (proposed.get("mechanisms") or {}) or type(count) is not int or count < 1
                   for name, count in allocation.items()) or sum(allocation.values()) != 4):
        raise ValueError("PPA_DISCOVERY_ALLOCATION_INVALID")
    root = await db.get(ExperimentRun, (task.config or {}).get("budget_root_run_id"))
    root_config = (((root.config_snapshot or {}).get("task") or {}).get("config") or {}) if root else {}
    if not root or not new_fields_have_evidence(proposed, root_config.get("ppa_manifest") or {}):
        raise ValueError("PPA_FIELD_EVIDENCE_REQUIRED")
    config = {**task.config, "ppa_manifest": proposed, "ppa_local_expansion": False}
    from backend.services.theme_ppa import validate_theme_task
    candidate = SimpleNamespace(region=task.region, universe=task.universe,
                                dataset_strategy=task.dataset_strategy,
                                target_datasets=list(datasets), config=config)
    reason = validate_theme_task(candidate)
    if reason:
        raise ValueError(reason)
    task.target_datasets = list(datasets)
    task.config = config
    await db.commit()
    return {"task_id": task_id, "previous_run_id": run.id, "status": "STOPPED"}
