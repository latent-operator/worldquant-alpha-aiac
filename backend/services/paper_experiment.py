"""Bounded, checkpointed AIAC worker protocol for news17 paper proxies."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from backend.alpha_semantic_validator import AlphaSemanticValidator, compute_expression_hash
from backend.agents.graph.nodes.validation import _validate_task_constraints
from backend.alpha_scoring import should_optimize
from backend.models import Alpha, ResearchCandidate, TraceStep
from backend.services.research_audit import (
    FAMILY_ID, ResearchBudgetBrain, ResearchLedger, constrained_mutations,
    paired_improvements, paper_matrix,
)


APPROVED_FIELDS = frozenset({
    "nws17_d1_event_sentiment_score", "nws17_d1_event_relevance",
    "nws17_d1_event_similarity_days", "nws17_d1_rp_story_event_count",
    "nws17_d1_qmb", "nws17_d1_ssc", "nws17_d1_qep", "nws17_d1_ber",
    "nws17_d1_acb", "nws17_d1_relevance", "nws17_d1_rp_story_event_index",
})
APPROVED_OPERATORS = frozenset({"rank", "vec_avg", "ts_mean", "ts_rank", "ts_sum"})
PROTOCOL = "paper_news17_ablation_v1"


def protocol_settings(task) -> dict:
    config = task.config or {}
    return {
        "region": task.region, "universe": task.universe,
        "delay": int(config.get("delay", 1)),
        "decay": int(config.get("decay", 4)),
        "neutralization": config.get("neutralization", "SUBINDUSTRY"),
        "truncation": float(config.get("truncation", 0.08)),
        "test_period": config.get("test_period", "P2Y0M"),
    }


def validate_protocol_task(task, fields: list[dict]) -> str | None:
    config = task.config or {}
    settings = protocol_settings(task)
    if (settings["region"], settings["universe"], settings["delay"]) != ("USA", "TOP3000", 1):
        return "PAPER_REGION_UNIVERSE_DELAY_MISMATCH"
    if (settings["decay"], settings["neutralization"],
            settings["truncation"], settings["test_period"]) != (4, "SUBINDUSTRY", 0.08, "P2Y0M"):
        return "PAPER_BASELINE_SETTINGS_MISMATCH"
    if task.dataset_strategy != "SPECIFIC" or task.target_datasets != ["news17"]:
        return "PAPER_DATASET_MISMATCH"
    if set(config.get("research_only_fields") or []) != APPROVED_FIELDS:
        return "PAPER_APPROVED_FIELDS_MISMATCH"
    available = {str(row.get("id") or row.get("name") or "") for row in fields}
    if not APPROVED_FIELDS.issubset(available):
        return "PAPER_FIELDS_MISSING_FROM_CATALOG"
    if int(config.get("research_request_budget", 0)) != 40:
        return "PAPER_BUDGET_MUST_BE_40"
    return None


def _rows_for_seed(candidates: list[ResearchCandidate]) -> list[dict]:
    return [{
        "id": item.id, "arm": item.arm, "group": item.experiment_group,
        "pair_id": item.pair_id, "window": item.window,
        "expression": item.expression, "metrics": item.metrics or {},
    } for item in candidates if item.request_status == "RESULT"]


async def run_paper_dispatch(db, brain, task, run, fields: list[dict]) -> dict:
    """Process at most five matrix/GA candidates, then yield the same run."""
    config_error = validate_protocol_task(task, fields)
    if config_error:
        task.status = run.status = "PAUSED"
        run.block_reason = config_error
        await db.commit()
        return {"done": True, "reason": config_error}

    config = task.config or {}
    settings = protocol_settings(task)
    checkpoint = dict(run.checkpoint or {})
    cursor = int(checkpoint.get("paper_cursor", 0))
    matrix = paper_matrix()
    if cursor < len(matrix):
        cases = matrix[cursor: min(cursor + 5, len(matrix))]
        phase = "MATRIX"
    else:
        rows = (await db.execute(select(ResearchCandidate).where(
            ResearchCandidate.run_id == run.id,
        ))).scalars().all()
        genetic_requests = sum(
            row.source == "GENETIC" and row.request_status in
            {"RESERVED", "REQUESTED", "RESULT", "FAILED", "UNKNOWN"} for row in rows
        )
        if genetic_requests >= 15:
            task.status = run.status = "COMPLETED"
            run.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
            run.checkpoint = {**checkpoint, "paper_stop_reason": "GENETIC_REQUEST_CAP_REACHED"}
            await db.commit()
            return {"done": True, "reason": "GENETIC_REQUEST_CAP_REACHED"}
        seeds = paired_improvements(_rows_for_seed(rows))
        attempted = {row.expression for row in rows}
        mutations = constrained_mutations(seeds, limit=200)
        cases = [case for case in mutations if case["expression"] not in attempted][
            : min(5, 15 - genetic_requests)
        ]
        phase = "GENETIC"

    if not cases:
        reason = "NO_QUALIFIED_GENETIC_SEEDS" if cursor == len(matrix) else "CANDIDATES_EXHAUSTED"
        task.status = run.status = "COMPLETED"
        run.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        run.checkpoint = {**checkpoint, "paper_stop_reason": reason}
        await db.commit()
        return {"done": True, "reason": reason}

    ledger = ResearchLedger(task.id, run.id, 40, FAMILY_ID)
    audited_brain = brain if isinstance(brain, ResearchBudgetBrain) else ResearchBudgetBrain(brain, ledger)
    validator = AlphaSemanticValidator(fields=fields, strict_field_check=True)
    valid_cases = []
    rejected = {}
    for case in cases:
        expression = case["expression"]
        used = validator.validate(expression)
        reason = None
        stage = None
        if not set(used.used_fields).issubset(APPROVED_FIELDS):
            stage, reason = "FIELD", "FIELD_NOT_APPROVED"
        elif not {str(op).lower() for op in used.used_operators}.issubset(APPROVED_OPERATORS):
            stage, reason = "OPERATOR", "OPERATOR_NOT_APPROVED"
        elif not used.valid:
            stage, reason = "SEMANTIC", "SEMANTIC_VALIDATION_FAILED"
        else:
            errors = _validate_task_constraints(
                expression=expression, allowed_fields=list(APPROVED_FIELDS),
                task_config=config, fields=fields,
            )
            if errors:
                stage, reason = "PAPER_CONSTRAINT", "PAPER_CONSTRAINT_FAILED"
        if reason:
            await ledger.reject(expression, settings, phase, stage, reason, case)
            rejected[stage] = rejected.get(stage, 0) + 1
        else:
            audited_brain.register(expression, **{key: value for key, value in case.items()
                                                  if key != "expression"})
            valid_cases.append(case)

    results = []
    if valid_cases:
        with audited_brain.source(phase):
            results = await audited_brain.simulate_batch(
                [case["expression"] for case in valid_cases], **settings,
            )

    trace = TraceStep(
        task_id=task.id, run_id=run.id, step_type="RESEARCH_BATCH",
        step_order=cursor + 1, iteration=cursor // 5 + 1,
        input_data={"phase": phase, "case_count": len(cases)},
        output_data={"validated": len(valid_cases), "rejected": rejected,
                     "wq_results": sum(bool(r.get("alpha_id")) for r in results)},
        status="SUCCESS",
    )
    db.add(trace)
    await db.flush()

    from backend.agents.mining_agent import MiningAgent
    agent = MiningAgent(db, audited_brain)
    pass_count = 0
    for case, result in zip(valid_cases, results):
        alpha_id = result.get("alpha_id")
        if not alpha_id:
            continue
        metrics = dict(result.get("metrics") or {})
        metrics.update({
            "checks": result.get("checks") or metrics.get("checks") or [],
            "can_submit": result.get("can_submit", False),
            "stage": result.get("stage") if result.get("stage") in {"IS", "OS"} else None,
            "status": result.get("status") if result.get("status") in {"UNSUBMITTED", "SUBMITTED"} else None,
            "_settings": settings,
            "_paper_vendor_proxy": True,
            "_paper_family": FAMILY_ID,
        })
        strict_status = await agent._quality_status_for_optimization_result(
            task=task, expression=case["expression"], metrics=metrics,
            fields=fields, alpha_id=alpha_id,
        )
        can_optimize, _ = should_optimize({"is": metrics, "os": result.get("os") or {}})
        quality_status = "PASS" if strict_status == "PASS" else "OPTIMIZE" if can_optimize else "FAIL"
        pass_count += quality_status == "PASS"
        alpha = Alpha(
            task_id=task.id, run_id=run.id, trace_step_id=trace.id,
            alpha_id=alpha_id, expression=case["expression"],
            expression_hash=compute_expression_hash(case["expression"]),
            hypothesis="arXiv 2512.19484 vendor-field proxy; matched sentiment control",
            region=task.region, universe=task.universe, dataset_id="news17",
            delay=settings["delay"], decay=settings["decay"],
            neutralization=settings["neutralization"], truncation=settings["truncation"],
            settings=settings, status="simulated", stage="IS",
            quality_status=quality_status, is_sharpe=metrics.get("sharpe"),
            is_fitness=metrics.get("fitness"), is_turnover=metrics.get("turnover"),
            is_margin=metrics.get("margin"), checks=metrics.get("checks"),
            is_metrics=metrics, os_metrics=result.get("os") or {}, metrics=metrics,
        )
        db.add(alpha)
        candidate = (await db.execute(select(ResearchCandidate).where(
            ResearchCandidate.run_id == run.id,
            ResearchCandidate.wq_alpha_id == alpha_id,
        ).limit(1))).scalar_one_or_none()
        if candidate:
            candidate.quality_status = quality_status
            candidate.gate_failures = metrics.get("_strict_gate_failures") or []
            candidate.trace_step_id = trace.id
            candidate.metrics = {**(candidate.metrics or {}),
                                 "os_sharpe": (result.get("os") or {}).get("sharpe"),
                                 "os_fitness": (result.get("os") or {}).get("fitness")}

    task.progress_current += pass_count
    cursor += len(cases)
    run.checkpoint = {**checkpoint, "paper_cursor": cursor, "paper_phase": phase}
    await db.commit()
    return {"done": False, "cursor": cursor, "pass_count": pass_count,
            "wq_results": sum(bool(r.get("alpha_id")) for r in results)}
