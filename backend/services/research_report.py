"""Bounded candidate audit pages and WQ quality-grounded paper reports."""

from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import func, select

from backend.models import Alpha, ExperimentRun, ResearchCandidate
from backend.services.credentials_service import CredentialsService


def candidate_public(row: ResearchCandidate) -> dict:
    return {
        "id": row.id, "run_id": row.run_id, "family_id": row.family_id,
        "source": row.source, "arm": row.arm, "experiment_group": row.experiment_group,
        "pair_id": row.pair_id, "window": row.window,
        "parent_candidate_id": row.parent_candidate_id,
        "expression": CredentialsService.sanitize_error(row.expression),
        "expression_hash": row.expression_hash,
        "settings_hash": row.settings_hash, "settings": row.settings,
        "validation_status": row.validation_status,
        "rejection_stage": row.rejection_stage, "rejection_reason": row.rejection_reason,
        "request_status": row.request_status, "wq_alpha_id": row.wq_alpha_id,
        "quality_status": row.quality_status, "gate_failures": row.gate_failures or [],
        "metrics": row.metrics or {}, "trace_step_id": row.trace_step_id,
        "created_at": row.created_at,
    }


async def candidate_page(db, run_id: int, limit: int, offset: int) -> dict:
    if await db.get(ExperimentRun, run_id) is None:
        raise ValueError("Run not found")
    total = (await db.execute(select(func.count(ResearchCandidate.id)).where(
        ResearchCandidate.run_id == run_id,
    ))).scalar_one()
    rows = (await db.execute(select(ResearchCandidate).where(
        ResearchCandidate.run_id == run_id,
    ).order_by(ResearchCandidate.id).limit(limit).offset(offset))).scalars().all()
    return {"items": [candidate_public(row) for row in rows], "total": total,
            "limit": limit, "offset": offset, "has_more": offset + len(rows) < total}


def paired_deltas(rows: list[ResearchCandidate]) -> list[dict]:
    controls = {row.pair_id: row for row in rows if row.arm == "CONTROL"
                and row.request_status == "RESULT"}
    pairs = []
    for row in rows:
        if row.arm != "TREATMENT":
            continue
        control = controls.get(row.pair_id)
        metrics = row.metrics or {}
        baseline = (control.metrics or {}) if control else {}
        deltas = {}
        for metric in ("sharpe", "fitness", "os_sharpe", "os_fitness"):
            try:
                left, right = metrics.get(metric), baseline.get(metric)
                deltas[metric] = float(left) - float(right) if left is not None and right is not None else None
            except (TypeError, ValueError):
                deltas[metric] = None
        pairs.append({
            "pair_id": row.pair_id, "group": row.experiment_group,
            "window": row.window, "control_candidate_id": control.id if control else None,
            "treatment_candidate_id": row.id,
            "control_wq_alpha_id": control.wq_alpha_id if control else None,
            "treatment_wq_alpha_id": row.wq_alpha_id,
            "deltas": deltas, "quality_status": row.quality_status,
            "failed_gates": row.gate_failures or [],
        })
    return pairs


async def research_report(db, run_id: int) -> dict:
    run = await db.get(ExperimentRun, run_id)
    if run is None:
        raise ValueError("Run not found")
    rows = (await db.execute(select(ResearchCandidate).where(
        ResearchCandidate.run_id == run_id,
    ).order_by(ResearchCandidate.id))).scalars().all()
    wq_rows = (await db.execute(select(Alpha).where(
        Alpha.run_id == run_id, Alpha.alpha_id.is_not(None),
    ))).scalars().all()
    statuses = Counter(row.request_status for row in rows)
    rejections = Counter(f"{row.rejection_stage}:{row.rejection_reason}" for row in rows
                         if row.validation_status == "REJECTED")
    quality = Counter(alpha.quality_status for alpha in wq_rows)
    config = ((run.config_snapshot or {}).get("task") or {}).get("config") or {}
    return {
        "run_id": run.id, "task_id": run.task_id, "status": run.status,
        "source": "AIAC persisted candidate audit and Alpha rows",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "audit_available": bool(rows),
        "paper_transfer": "VENDOR_FIELD_PROXY_NOT_PAPER_REPRODUCTION" if rows else None,
        "budget": {"limit": config.get("research_request_budget"),
                   "reserved": sum(statuses[name] for name in ("RESERVED", "REQUESTED", "RESULT", "FAILED", "UNKNOWN")),
                   "unknown_pending_reconciliation": statuses["UNKNOWN"]},
        "candidate_funnel": {
            "generated": len(rows),
            "validation_passed": sum(row.validation_status == "VALID" for row in rows),
            "send_attempts_or_uncertain": sum(statuses[name] for name in ("REQUESTED", "RESULT", "FAILED", "UNKNOWN")),
            "reserved_not_yet_sent": statuses["RESERVED"],
            "wq_results": statuses["RESULT"],
            "saved_wq_results": len(wq_rows),
            "quality_pass": quality["PASS"],
        },
        "quality_counts": dict(quality), "request_counts": dict(statuses),
        "rejection_counts": dict(rejections),
        "genetic_funnel": {
            "generated": sum(row.source == "GENETIC" for row in rows),
            "requested": sum(row.source == "GENETIC" and row.request_status in
                             {"REQUESTED", "RESULT", "FAILED", "UNKNOWN"} for row in rows),
            "wq_results": sum(row.source == "GENETIC" and row.request_status == "RESULT" for row in rows),
            "rejections": {key: count for key, count in rejections.items()
                           if any(row.source == "GENETIC" and
                                  f"{row.rejection_stage}:{row.rejection_reason}" == key for row in rows)},
        },
        "paired_deltas": paired_deltas(rows),
        "stop_reason": (run.checkpoint or {}).get("paper_stop_reason"),
    }
