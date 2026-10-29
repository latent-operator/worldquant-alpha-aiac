"""Frozen, bounded Power Pool research protocol."""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import re
from math import isfinite
from urllib.parse import urlparse

from sqlalchemy import select

from backend.alpha_semantic_validator import AlphaSemanticValidator, compute_expression_hash
from backend.models import Alpha, ResearchCandidate, TraceStep
from backend.services.ppa_eligibility import evaluate_ppa_candidate
from backend.services.research_audit import ResearchBudgetBrain, ResearchLedger, safe_result


PHASE_LIMITS = {"discovery": 24, "refinement": 40, "confirmation": 16}


def account_quota_verified(eligibility: dict) -> bool:
    """Accept a verified balance or an account-owner confirmed no-cap policy."""
    try:
        remaining = int(eligibility.get("quota_remaining") or 0)
    except (TypeError, ValueError):
        remaining = 0
    return remaining >= 80 or (
        eligibility.get("quota_mode") == "NO_FIXED_LIMIT"
        and eligibility.get("quota_source") == "account_owner"
    )


def candidate_plan_hash(manifest: dict) -> str:
    """Bind Cockpit output to the frozen theme constraints and candidate cases."""
    payload = {key: value for key, value in manifest.items()
               if key != "candidate_generation"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _timestamp(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def validate_theme_task(task) -> str | None:
    """Fail closed before any theme-specific WQ request."""
    config = task.config or {}
    manifest = config.get("ppa_manifest") or {}
    if config.get("continuous_mode") is True:
        return "PPA_CONTINUOUS_FORBIDDEN"
    try:
        budget = int(config.get("research_request_budget") or 0)
    except (TypeError, ValueError):
        budget = 0
    if budget != 80:
        return "PPA_BUDGET_MUST_BE_80"
    if not str(manifest.get("theme_id") or manifest.get("theme_name") or "").strip():
        return "PPA_THEME_ID_MISSING"
    if manifest.get("verified_source") != "platform":
        return "PPA_THEME_NOT_PLATFORM_VERIFIED"
    source = urlparse(str(manifest.get("platform_source_url") or ""))
    if source.scheme != "https" or source.hostname != "platform.worldquantbrain.com":
        return "PPA_THEME_NOT_PLATFORM_VERIFIED"
    eligibility = manifest.get("account_eligibility") or {}
    verified_at = _timestamp(eligibility.get("verified_at"))
    if (eligibility.get("power_pool_access") is not True or not verified_at
            or verified_at.tzinfo is None or not account_quota_verified(eligibility)):
        return "PPA_ACCOUNT_UNVERIFIED"
    start, end = _timestamp(manifest.get("starts_at")), _timestamp(manifest.get("ends_at"))
    if not start or not end or start.tzinfo is None or end.tzinfo is None or start >= end:
        return "PPA_THEME_WINDOW_INVALID"
    try:
        delay = int(manifest.get("delay") or -1)
    except (TypeError, ValueError):
        delay = -1
    if (task.region, task.universe, delay) != (
        manifest.get("region"), manifest.get("universe"), 1,
    ):
        return "PPA_SCOPE_MISMATCH"
    datasets = manifest.get("datasets") or {}
    if (task.dataset_strategy != "SPECIFIC" or not 1 <= len(datasets) <= 2
            or set(task.target_datasets or []) != set(datasets)):
        return "PPA_DATASET_MISMATCH"
    if (not all(isinstance(fields, list) and fields for fields in datasets.values())
            or not isinstance(manifest.get("settings"), dict)):
        return "PPA_MANIFEST_INCOMPLETE"
    if (set(manifest.get("dataset_categories") or {}) != set(datasets)
            or not all(isinstance(value, str) and value.strip()
                       for value in (manifest.get("dataset_categories") or {}).values())
            or not isinstance(manifest.get("allowed_operators"), list)
            or not manifest["allowed_operators"]):
        return "PPA_MANIFEST_INCOMPLETE"
    if (not isinstance(manifest.get("eligible_classification_ids"), list)
            or not isinstance(manifest.get("required_pre_submit_checks"), list)
            or not manifest["required_pre_submit_checks"]):
        return "PPA_MANIFEST_INCOMPLETE"
    try:
        if (not 0 < float(manifest["ppa_corr_max"]) < 1
                or not 0 < float(manifest["turnover_max"]) <= 1
                or not 1 <= int(manifest["min_positions"])
                or not 1 <= int(manifest["max_fields"]) <= 3
                or not 1 <= int(manifest["max_operator_count"]) <= 8):
            return "PPA_MANIFEST_INCOMPLETE"
    except (KeyError, TypeError, ValueError):
        return "PPA_MANIFEST_INCOMPLETE"
    if (any(not isinstance(manifest.get(phase), list) for phase in PHASE_LIMITS)
            or len(manifest.get("discovery") or []) != 24
            or any(len(manifest.get(phase) or []) > limit
                   for phase, limit in PHASE_LIMITS.items())):
        return "PPA_PHASE_LIMIT"
    ids, pairs, request_signatures = set(), {}, set()
    base_settings = manifest["settings"]
    if any(not isinstance(value, (str, int, float, bool)) for value in base_settings.values()):
        return "PPA_CASE_SETTINGS_INVALID"
    if any(key in base_settings for key in ("region", "universe", "delay")):
        return "PPA_CASE_SETTINGS_INVALID"
    confirmation_settings = manifest.get("confirmation_settings") or {}
    if not isinstance(confirmation_settings, dict):
        return "PPA_CASE_SETTINGS_INVALID"
    for phase in PHASE_LIMITS:
        for case in manifest.get(phase) or []:
            case_id = case.get("case_id")
            if (not case_id or case_id in ids or not case.get("expression")
                    or case.get("dataset_id") not in datasets):
                return "PPA_CASE_INVALID"
            ids.add(case_id)
            overrides = case.get("settings") or {}
            if not isinstance(overrides, dict) or any(key in overrides for key in (
                    "region", "universe", "delay")):
                return "PPA_CASE_SETTINGS_INVALID"
            if phase != "confirmation" and overrides:
                return "PPA_CASE_SETTINGS_INVALID"
            if any(key not in confirmation_settings
                   or not isinstance(confirmation_settings[key], list)
                   or value not in confirmation_settings[key]
                   or not isinstance(value, (str, int, float, bool))
                   for key, value in overrides.items()):
                return "PPA_CASE_SETTINGS_INVALID"
            signature = (case["expression"].strip(), tuple(sorted({**base_settings, **overrides}.items())))
            if signature in request_signatures:
                return "PPA_DUPLICATE_CASE_REQUEST"
            request_signatures.add(signature)
            if phase == "discovery":
                pair_id = case.get("pair_id")
                arm = case.get("arm")
                if not pair_id or arm not in {"CONTROL", "TREATMENT"}:
                    return "PPA_CASE_INVALID"
                pairs.setdefault(pair_id, Counter())[arm] += 1
            elif case.get("parent_case_id") not in ids:
                return "PPA_PARENT_INVALID"
    if len(pairs) != 6 or any(counts != {"CONTROL": 1, "TREATMENT": 3}
                              for counts in pairs.values()):
        return "PPA_DISCOVERY_DESIGN_INVALID"
    controls = {case["pair_id"]: case for case in manifest["discovery"]
                if case["arm"] == "CONTROL"}
    for case in manifest["discovery"]:
        if case["arm"] != "TREATMENT":
            continue
        baseline = controls[case["pair_id"]]
        if case["dataset_id"] != baseline["dataset_id"]:
            return "PPA_DISCOVERY_NOT_SINGLE_FACTOR"
        token_pattern = r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|\S"
        left = re.findall(token_pattern, baseline["expression"])
        right = re.findall(token_pattern, case["expression"])
        if len(left) != len(right) or sum(a != b for a, b in zip(left, right)) != 1:
            return "PPA_DISCOVERY_NOT_SINGLE_FACTOR"
    generation = manifest.get("candidate_generation") or {}
    prepared_at = _timestamp(generation.get("prepared_at"))
    if (generation.get("source") != "cockpit_local_responses"
            or generation.get("model") != "gpt-6-luna"
            or not prepared_at or prepared_at.tzinfo is None):
        return "PPA_CANDIDATE_SOURCE_INVALID"
    if generation.get("plan_sha256") != candidate_plan_hash(manifest):
        return "PPA_CANDIDATE_PLAN_CHANGED"
    return None


def _complete(metrics):
    try:
        return all(metrics.get(key) is not None and isfinite(float(metrics[key]))
                   for key in ("sharpe", "fitness", "turnover", "margin"))
    except (TypeError, ValueError):
        return False


def phase_cases(manifest: dict, phase: str, rows: list) -> list[dict]:
    """Release frozen cases only when their parent has supporting evidence."""
    cases = manifest.get(phase) or []
    if phase == "discovery":
        return cases
    definitions = {case["case_id"]: case for name in PHASE_LIMITS
                   for case in manifest.get(name) or []}
    by_case = {row.experiment_group: row for row in rows if row.request_status == "RESULT"}
    controls = {row.pair_id: row for row in rows
                if row.arm == "CONTROL" and row.request_status == "RESULT"}
    eligible = []
    for case in cases:
        parent = definitions.get(case.get("parent_case_id"))
        parent_row = by_case.get(parent.get("case_id")) if parent else None
        if not parent_row or not _complete(parent_row.metrics or {}):
            continue
        if phase == "refinement":
            control = controls.get(parent.get("pair_id"))
            if (not control or not _complete(control.metrics or {})
                    or not all(float(parent_row.metrics[key]) > float(control.metrics[key])
                               for key in ("sharpe", "fitness"))):
                continue
        elif (parent_row.result_snapshot or {}).get("ppa_evaluation", {}).get("status") == "PPA_REJECTED":
            continue
        eligible.append(case)
    return eligible


async def run_theme_dispatch(db, brain, task, run, fields_by_dataset: dict) -> dict:
    """Process a bounded slice of a frozen theme experiment."""
    reason = validate_theme_task(task)
    if reason is None:
        manifest = (task.config or {})["ppa_manifest"]
        now = datetime.now(timezone.utc)
        if now < _timestamp(manifest["starts_at"]):
            reason = "PPA_THEME_NOT_ACTIVE"
        elif now > _timestamp(manifest["ends_at"]):
            reason = "PPA_THEME_ENDED"
    if reason:
        task.status = run.status = "PAUSED"
        run.block_reason = reason
        await db.commit()
        return {"done": True, "reason": reason}

    if any(not fields_by_dataset.get(dataset) for dataset in manifest["datasets"]):
        task.status = run.status = "PAUSED"
        run.block_reason = "PPA_FIELDS_NOT_AVAILABLE"
        await db.commit()
        return {"done": True, "reason": run.block_reason}

    checkpoint = dict(run.checkpoint or {})
    checkpoint.pop("ppa_stop_reason", None)
    phase = checkpoint.get("ppa_phase", "discovery")
    cursor = int(checkpoint.get("ppa_cursor", 0))
    phases = list(PHASE_LIMITS)
    rows = (await db.execute(select(ResearchCandidate).where(
        ResearchCandidate.run_id == run.id,
    ))).scalars().all()
    while cursor >= len(manifest[phase]):
        index = phases.index(phase)
        if index == len(phases) - 1:
            reason = "PPA_CASES_EXHAUSTED"
            task.status = run.status = "COMPLETED"
            run.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
            run.checkpoint = {**checkpoint, "ppa_stop_reason": reason}
            await db.commit()
            return {"done": True, "reason": reason}
        phase, cursor = phases[index + 1], 0
        if phase_cases(manifest, phase, rows) == []:
            reason = "PPA_NO_QUALIFIED_SEEDS" if phase == "refinement" else "PPA_NO_FINALISTS"
            task.status = run.status = "COMPLETED"
            run.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
            run.checkpoint = {**checkpoint, "ppa_stop_reason": reason}
            await db.commit()
            return {"done": True, "reason": reason}

    batch = manifest[phase][cursor:cursor + 5]
    permitted = {case["case_id"] for case in phase_cases(manifest, phase, rows)}
    selected = [case for case in batch if case["case_id"] in permitted]
    theme_label = manifest.get("theme_id") or manifest["theme_name"]
    ledger = ResearchLedger(
        task.id, run.id, 80, f"theme_ppa_v1:{theme_label}",
        dedup_task_ids=(task.config or {}).get("dedup_task_ids", []),
    )
    audited_brain = brain if isinstance(brain, ResearchBudgetBrain) else ResearchBudgetBrain(brain, ledger)
    from backend.agents.graph.nodes.evaluation import _operator_call_count
    from backend.agents.mining_agent import MiningAgent
    agent = MiningAgent(db, audited_brain)
    results = []
    rejected = 0
    rate_limited = False
    processed_cursor = cursor + len(batch)
    for case in selected:
        expression = case["expression"]
        dataset_id = case["dataset_id"]
        fields = fields_by_dataset[dataset_id]
        used = AlphaSemanticValidator(fields=fields, strict_field_check=True).validate(expression)
        settings = {**manifest["settings"], **(case.get("settings") or {}),
                    "region": task.region, "universe": task.universe,
                    "delay": manifest["delay"]}
        allowed_fields = set(manifest["datasets"][dataset_id])
        if (not used.valid or not set(used.used_fields).issubset(allowed_fields)
                or not set(used.used_operators).issubset(
                    {name.lower() for name in manifest["allowed_operators"]})
                or len(set(used.used_fields)) > int(manifest["max_fields"])
                or _operator_call_count(expression) > int(manifest["max_operator_count"])):
            await ledger.reject(expression, settings, phase.upper(),
                                "PPA_CONSTRAINT", "PPA_CASE_INVALID", case)
            rejected += 1
            continue
        parent = next((row for row in rows
                       if row.experiment_group == case.get("parent_case_id")), None)
        metadata = {"group": case.get("case_id"), "arm": case.get("arm"),
                    "pair_id": case.get("pair_id"),
                    "parent_candidate_id": parent.id if parent else None}
        audited_brain.register(expression, **metadata)
        with audited_brain.source(phase.upper()):
            result = await audited_brain.simulate_alpha(expression, **settings)
        results.append((case, settings, fields, result))
        if result.get("http_status") == 429:
            rate_limited = True
            processed_cursor = cursor + batch.index(case) + 1
            break

    trace = TraceStep(
        task_id=task.id, run_id=run.id, step_type="PPA_RESEARCH_BATCH",
        step_order=cursor + 1, iteration=sum(len(manifest[p]) for p in phases[:phases.index(phase)]) + cursor + 1,
        input_data={"phase": phase, "case_count": len(batch)},
        output_data={"selected": len(selected), "rejected": rejected,
                     "wq_results": sum(bool(item[3].get("alpha_id")) for item in results)},
        status="SUCCESS",
    )
    db.add(trace)
    await db.flush()

    ready = 0
    for case, settings, fields, result in results:
        alpha_id = result.get("alpha_id")
        if not alpha_id:
            continue
        try:
            corr_payload = await audited_brain.check_power_pool_correlation(alpha_id)
        except Exception:
            corr_payload = {}
        corr = corr_payload.get("max") if isinstance(corr_payload, dict) else None
        snapshot = safe_result(result)
        evaluation = evaluate_ppa_candidate(snapshot, manifest, corr)
        metrics = dict(result.get("metrics") or {})
        metrics.update({"checks": result.get("checks") or [],
                        "can_submit": result.get("can_submit", False),
                        "stage": result.get("stage"), "status": result.get("status")})
        strict_status = await agent._quality_status_for_optimization_result(
            task=task, expression=case["expression"], metrics=metrics,
            fields=fields, alpha_id=alpha_id,
        )
        db.add(Alpha(
            task_id=task.id, run_id=run.id, trace_step_id=trace.id,
            alpha_id=alpha_id, expression=case["expression"],
            expression_hash=compute_expression_hash(case["expression"]),
            hypothesis=f"Frozen Power Pool theme {theme_label} case {case['case_id']}",
            region=task.region, universe=task.universe, dataset_id=case["dataset_id"],
            delay=settings["delay"], decay=settings["decay"],
            neutralization=settings["neutralization"], truncation=settings["truncation"],
            settings=settings, status="simulated", stage=result.get("stage") or "IS",
            quality_status=strict_status, is_sharpe=metrics.get("sharpe"),
            is_fitness=metrics.get("fitness"), is_turnover=metrics.get("turnover"),
            is_margin=metrics.get("margin"), checks=metrics.get("checks"),
            is_metrics=metrics, os_metrics=result.get("os") or {}, metrics=metrics,
        ))
        candidate = (await db.execute(select(ResearchCandidate).where(
            ResearchCandidate.run_id == run.id,
            ResearchCandidate.wq_alpha_id == alpha_id,
        ).limit(1))).scalar_one_or_none()
        if candidate:
            candidate.quality_status = strict_status
            candidate.gate_failures = metrics.get("_strict_gate_failures") or []
            candidate.trace_step_id = trace.id
            candidate.result_snapshot = {**(candidate.result_snapshot or {}),
                                         "ppa_evaluation": evaluation,
                                         "power_pool_correlation": corr}
        if evaluation["status"] == "PPA_READY":
            ready += 1

    cursor = processed_cursor
    task.progress_current += ready
    run.checkpoint = {**checkpoint, "ppa_phase": phase, "ppa_cursor": cursor}
    if ready:
        task.status = run.status = "COMPLETED"
        run.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        run.checkpoint = {**run.checkpoint, "ppa_stop_reason": "PPA_READY"}
    elif rate_limited:
        task.status = run.status = "PAUSED"
        run.block_reason = "PPA_RATE_LIMIT"
        run.checkpoint = {**run.checkpoint, "ppa_stop_reason": "PPA_RATE_LIMIT"}
    await db.commit()
    reason = "PPA_READY" if ready else "PPA_RATE_LIMIT" if rate_limited else None
    return {"done": bool(ready or rate_limited), "reason": reason,
            "phase": phase, "cursor": cursor, "ready": ready}
