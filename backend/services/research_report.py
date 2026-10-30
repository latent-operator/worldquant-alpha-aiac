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
        "ppa_evaluation": (row.result_snapshot or {}).get("ppa_evaluation"),
        "platform_ppa_evaluation": (row.result_snapshot or {}).get("platform_ppa_evaluation"),
        "robustness_review": (row.result_snapshot or {}).get("robustness_review"),
        "classifications": (row.result_snapshot or {}).get("classifications") or [],
        "themes": (row.result_snapshot or {}).get("themes") or [],
        "platform_checks": (row.result_snapshot or {}).get("checks") or [],
        "power_pool_correlation": (row.result_snapshot or {}).get("power_pool_correlation"),
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
    statuses = Counter(dict((await db.execute(
        select(ResearchCandidate.request_status, func.count(ResearchCandidate.id))
        .where(ResearchCandidate.run_id == run_id)
        .group_by(ResearchCandidate.request_status)
    )).all()))
    total = sum(statuses.values())
    valid = (await db.execute(select(func.count(ResearchCandidate.id)).where(
        ResearchCandidate.run_id == run_id, ResearchCandidate.validation_status == "VALID",
    ))).scalar_one()
    rejection_rows = (await db.execute(select(
        ResearchCandidate.rejection_stage, ResearchCandidate.rejection_reason,
        func.count(ResearchCandidate.id),
    ).where(ResearchCandidate.run_id == run_id,
            ResearchCandidate.validation_status == "REJECTED")
     .group_by(ResearchCandidate.rejection_stage, ResearchCandidate.rejection_reason))).all()
    rejections = {f"{stage}:{reason}": count for stage, reason, count in rejection_rows}
    quality = Counter(dict((await db.execute(
        select(Alpha.quality_status, func.count(Alpha.id))
        .where(Alpha.run_id == run_id, Alpha.alpha_id.is_not(None))
        .group_by(Alpha.quality_status)
    )).all()))
    saved_count = sum(quality.values())
    genetic = Counter(dict((await db.execute(
        select(ResearchCandidate.request_status, func.count(ResearchCandidate.id))
        .where(ResearchCandidate.run_id == run_id, ResearchCandidate.source == "GENETIC")
        .group_by(ResearchCandidate.request_status)
    )).all()))
    genetic_rejection_rows = (await db.execute(select(
        ResearchCandidate.rejection_stage, ResearchCandidate.rejection_reason,
        func.count(ResearchCandidate.id),
    ).where(ResearchCandidate.run_id == run_id,
            ResearchCandidate.source == "GENETIC",
            ResearchCandidate.validation_status == "REJECTED")
     .group_by(ResearchCandidate.rejection_stage, ResearchCandidate.rejection_reason))).all()
    best = (await db.execute(select(Alpha.id, Alpha.alpha_id, Alpha.is_sharpe,
                                    Alpha.is_fitness, Alpha.quality_status)
        .where(Alpha.run_id == run_id, Alpha.alpha_id.is_not(None),
               Alpha.is_sharpe.is_not(None))
        .order_by(Alpha.is_sharpe.desc()).limit(1))).first()
    sample = (await db.execute(select(Alpha.alpha_id).where(
        Alpha.run_id == run_id, Alpha.alpha_id.is_not(None),
    ).order_by(Alpha.id).limit(10))).scalars().all()
    config = ((run.config_snapshot or {}).get("task") or {}).get("config") or {}
    protocol = config.get("research_protocol")
    ppa_counts = Counter()
    ppa_phase_counts = {}
    robustness_reviews = []
    if protocol in {"theme_ppa_v1", "theme_ppa_v2", "theme_ppa_v3"}:
        snapshots = (await db.execute(select(
            ResearchCandidate.experiment_group, ResearchCandidate.result_snapshot).where(
            ResearchCandidate.run_id == run_id,
        ))).all()
        ppa_counts.update((snapshot or {}).get("ppa_evaluation", {}).get("status")
                          for _, snapshot in snapshots
                          if (snapshot or {}).get("ppa_evaluation", {}).get("status"))
        robustness_reviews = [{"parent_case_id": case_id, **snapshot["robustness_review"]}
                              for case_id, snapshot in snapshots
                              if (snapshot or {}).get("robustness_review")]
        phase_rows = (await db.execute(select(
            ResearchCandidate.source, ResearchCandidate.request_status,
            func.count(ResearchCandidate.id),
        ).where(ResearchCandidate.run_id == run_id).group_by(
            ResearchCandidate.source, ResearchCandidate.request_status,
        ))).all()
        for source, status, count in phase_rows:
            phase = str(source or "").lower()
            if phase not in {"discovery", "refinement", "confirmation"}:
                continue
            ppa_phase_counts.setdefault(phase, {})[status] = count
    paired = []
    if protocol in {"paper_news17_ablation_v1", "theme_ppa_v1", "theme_ppa_v2", "theme_ppa_v3"}:
        # Both fixed protocols are bounded; do not materialize continuous runs.
        paired_rows = (await db.execute(select(ResearchCandidate).where(
            ResearchCandidate.run_id == run_id,
        ).order_by(ResearchCandidate.id).limit(100))).scalars().all()
        paired = paired_deltas(paired_rows)
    budget_limit = None if config.get("continuous_mode") is True else config.get("research_request_budget")
    reserved = sum(statuses[name] for name in ("RESERVED", "REQUESTED", "RESULT", "FAILED", "UNKNOWN"))
    result = {
        "run_id": run.id, "task_id": run.task_id, "status": run.status,
        "source": "AIAC persisted candidate audit and Alpha rows",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "audit_available": total > 0,
        "paper_transfer": "VENDOR_FIELD_PROXY_NOT_PAPER_REPRODUCTION"
                          if total and protocol == "paper_news17_ablation_v1" else None,
        "budget": {"limit": budget_limit,
                   "remaining": max(0, int(budget_limit) - reserved) if budget_limit is not None else None,
                   "reserved": reserved,
                   "unknown_pending_reconciliation": statuses["UNKNOWN"]},
        "candidate_funnel": {
            "generated": total,
            "validation_passed": valid,
            "send_attempts_or_uncertain": sum(statuses[name] for name in ("REQUESTED", "RESULT", "FAILED", "UNKNOWN")),
            "reserved_not_yet_sent": statuses["RESERVED"],
            "wq_results": statuses["RESULT"],
            "saved_wq_results": saved_count,
            "quality_pass": quality["PASS"],
        },
        "quality_counts": dict(quality), "ppa_counts": dict(ppa_counts),
        "robustness_reviews": robustness_reviews,
        "ppa_phase_counts": ppa_phase_counts,
        "request_counts": dict(statuses),
        "rejection_counts": dict(rejections),
        "genetic_funnel": {
            "generated": sum(genetic.values()),
            "requested": sum(genetic[name] for name in ("REQUESTED", "RESULT", "FAILED", "UNKNOWN")),
            "wq_results": genetic["RESULT"],
            "rejections": {f"{stage}:{reason}": count
                           for stage, reason, count in genetic_rejection_rows},
        },
        "paired_deltas": paired,
        "wq_alpha_ids_sample": sample,
        "best_sharpe_alpha": ({"id": best.id, "wq_alpha_id": best.alpha_id,
                               "sharpe": best.is_sharpe, "fitness": best.is_fitness,
                               "quality_status": best.quality_status} if best else None),
        "ppa_v2_progress": (await _v2_progress(db, run, continuous=protocol == "theme_ppa_v3")) if protocol in {"theme_ppa_v2", "theme_ppa_v3"} else None,
        "stop_reason": (run.checkpoint or {}).get("ppa_stop_reason") if protocol in {"theme_ppa_v1", "theme_ppa_v2", "theme_ppa_v3"}
                       else (run.checkpoint or {}).get("paper_stop_reason"),
    }
    if protocol in {"theme_ppa_v2", "theme_ppa_v3"}:
        result["budget"] = result["ppa_v2_progress"]["campaign_budget"]
    return result


async def _v2_progress(db, run, *, continuous=False):
    from backend.services.ppa_progress import progress_report
    root_id = (((run.config_snapshot or {}).get("task") or {}).get("config") or {}).get("budget_root_run_id")
    root = await db.get(ExperimentRun, root_id) if root_id else None
    ids = sorted(set([run.id, root_id, *((root.checkpoint or {}).get("campaign_run_ids") or [])])) if root else [run.id]
    rows = (await db.execute(select(ResearchCandidate).where(ResearchCandidate.run_id.in_(ids))
                             .order_by(ResearchCandidate.id.desc() if continuous else ResearchCandidate.id)
                             .limit(1000))).scalars().all()
    result = progress_report(rows, run.id)
    reserved = (await db.execute(select(func.count(ResearchCandidate.id)).where(
        ResearchCandidate.run_id.in_(ids), ResearchCandidate.request_status.in_(
            ("RESERVED", "REQUESTED", "UNKNOWN", "FAILED", "RESULT"))))).scalar_one()
    result["campaign_budget"] = {"limit": None if continuous else 80, "reserved": reserved}
    result["campaign_budget"]["remaining"] = None if continuous else max(0, 80 - result["campaign_budget"]["reserved"])
    result["robustness_reviews"] = (run.checkpoint or {}).get("ppa_robustness_reviews") or {}
    return result
