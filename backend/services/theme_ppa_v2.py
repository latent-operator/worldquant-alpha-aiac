"""Stage-frozen feedback protocol with campaign accounting and per-result commits."""
import asyncio
import logging
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from sqlalchemy import select

from backend.models import MiningTask, ExperimentRun, ResearchCandidate, Alpha, TraceStep
from backend.alpha_semantic_validator import compute_expression_hash
from backend.services.ppa_feedback import (behavioral_signature, build_feedback, digest, freeze_batch,
                                           research_row, select_local_seeds, verify_batch_generation)
from backend.services.ppa_eligibility import evaluate_ppa_candidate
from backend.services.ppa_research_policy import evidence_metrics, seed_decision
from backend.services.research_audit import (ResearchLedger, ResearchBudgetBrain, safe_result, signature,
                                             is_uncertain_result)

CHARGED = {"RESERVED", "REQUESTED", "UNKNOWN", "FAILED", "RESULT"}
ACCEPTED = CHARGED | {"CACHED"}
PPA_RECONCILE_TIMEOUT_SECONDS = 1200
PPA_MULTISIM_RECONCILE_TIMEOUT_SECONDS = 1500
logger = logging.getLogger(__name__)


def needs_reconciliation(row):
    return (row.request_status in {"RESERVED", "REQUESTED", "UNKNOWN"}
            or row.request_status == "RESULT" and not (row.result_snapshot or {}).get("ppa_evaluation"))


def new_fields_have_evidence(base, historical):
    previous = historical.get("datasets") or {}
    metadata = base.get("field_metadata") or {}
    return all(metadata.get(dataset, {}).get(name)
               for dataset, fields in (base.get("datasets") or {}).items()
               for name in fields if name not in previous.get(dataset, []))


def runtime_fields_match(base, fields_by_dataset):
    """Require newly evidenced fields to match the account catalog before dispatch."""
    metadata = base.get("field_metadata")
    if metadata is None:
        return True
    for dataset, names in base.get("datasets", {}).items():
        catalog = {field.get("id"): field for field in fields_by_dataset.get(dataset, [])
                   if isinstance(field, dict)}
        for name in names:
            field = catalog.get(name) or {}
            actual = field.get("type") or field.get("field_type")
            expected = (metadata.get(dataset, {}).get(name) or {}).get("type")
            if not isinstance(actual, str) or actual.upper() != expected:
                return False
    return True


def validate_v2_manifest(task):
    config = task.config or {}
    base = config.get("ppa_manifest") or {}
    root = config.get("budget_root_run_id")
    if not isinstance(root, int) or isinstance(root, bool) or root <= 0:
        return "PPA_CAMPAIGN_ROOT_REQUIRED"
    mechanisms = base.get("mechanisms") or {}
    if not mechanisms or any(not isinstance(m, dict) or m.get("dataset_id") not in base["datasets"]
                             or not m.get("hypothesis") or not m.get("direction") for m in mechanisms.values()):
        return "PPA_MECHANISM_REGISTRY_REQUIRED"
    for mechanism in mechanisms.values():
        required = mechanism.get("required_fields")
        if required is not None and (not isinstance(required, list) or not required
                or any(not isinstance(name, str) or not name for name in required)
                or len(set(required)) != len(required)
                or not set(required).issubset(base["datasets"][mechanism["dataset_id"]])):
            return "PPA_MECHANISM_FIELDS_INVALID"
    identities = [(m["dataset_id"], m["hypothesis"].strip().lower(), m["direction"].lower()) for m in mechanisms.values()]
    if len(set(identities)) != len(identities):
        return "PPA_MECHANISM_ALIAS"
    historical_registry = base.get("historical_mechanism_registry") or {}
    if (not isinstance(historical_registry, dict) or set(historical_registry) & set(mechanisms)
            or any(not isinstance(m, dict) or not m.get("dataset_id") or not m.get("hypothesis")
                   or not m.get("direction") for m in historical_registry.values())):
        return "PPA_HISTORICAL_MECHANISM_INVALID"
    if any(name not in mechanisms and name not in historical_registry
           for name in (base.get("historical_mechanisms") or {}).values()):
        return "PPA_HISTORICAL_MECHANISM_INVALID"
    return None


async def campaign_rows(db, task, run):
    root_id = (task.config or {}).get("budget_root_run_id")
    root = await db.get(ExperimentRun, root_id)
    if root is None:
        raise ValueError("PPA_CAMPAIGN_ROOT_MISSING")
    root_config = ((root.config_snapshot or {}).get("task") or {}).get("config") or {}
    base = task.config["ppa_manifest"]
    if (root_config.get("research_protocol") not in {"theme_ppa_v1", "theme_ppa_v2"}
            or (root_config.get("ppa_manifest") or {}).get("theme_id") != base.get("theme_id")):
        raise ValueError("PPA_CAMPAIGN_SCOPE_MISMATCH")
    ids = sorted(set([root.id, run.id, *((root.checkpoint or {}).get("campaign_run_ids") or [])]))
    rows = (await db.execute(select(ResearchCandidate).where(ResearchCandidate.run_id.in_(ids))
                             .order_by(ResearchCandidate.id).execution_options(populate_existing=True))).scalars().all()
    return root, rows


async def feedback_context(db, task, run):
    continuous = task.config.get("research_protocol") == "theme_ppa_v3"
    root, rows = await campaign_rows(db, task, run)
    base = task.config["ppa_manifest"]
    historical = ((root.config_snapshot or {}).get("task") or {}).get("config", {}).get("ppa_manifest") or {}
    definitions = {c["case_id"]: c for phase in ("discovery", "refinement", "confirmation")
                   for c in historical.get(phase, [])}
    converted = []
    for row in rows:
        item = research_row(row)
        if row.run_id == root.id and not item["mechanism_id"]:
            case = definitions.get(row.experiment_group) or {}
            item["dataset_id"] = case.get("dataset_id")
            item["mechanism_id"] = (base.get("historical_mechanisms") or {}).get(case.get("pair_id"))
        converted.append(item)
    epoch = int((run.checkpoint or {}).get("ppa_epoch", 0))
    epoch_rows = [row for row in rows if (row.run_id == run.id if continuous else row.run_id != root.id) and
                  (not continuous or (row.result_snapshot or {}).get("research_case", {}).get("epoch", 0) == epoch)]
    request_phase_counts = dict(Counter(row.source.lower() for row in epoch_rows
                                        if row.request_status in CHARGED))
    phase_counts = (dict(Counter(row.source.lower() for row in epoch_rows
                                 if row.validation_status == "VALID" and row.request_status in ACCEPTED))
                    if continuous else request_phase_counts)
    remaining = None if continuous else max(0, 80 - sum(row.request_status in CHARGED for row in rows))
    checkpoint = run.checkpoint or {}
    phase_limit = 16 if continuous else 12
    context = build_feedback(converted, remaining, phase_counts, continuous=continuous,
                             phase_limit=phase_limit)
    context["epoch"] = epoch
    context["campaign_requests"] = sum(row.request_status in CHARGED for row in rows)
    context["request_phase_counts"] = request_phase_counts
    context["continuous"] = continuous
    context["local_expansion"] = continuous and task.config.get("ppa_local_expansion") is not False
    if continuous:
        context["strict_max_operator_count"] = int(task.config.get("max_operator_count", 5) or 5)
        parent_counts = Counter(row.parent_candidate_id for row in rows
                                if row.source == "REFINEMENT" and row.validation_status == "VALID"
                                and row.request_status in ACCEPTED and row.parent_candidate_id is not None)
        context["parent_total_counts"] = {str(key): count for key, count in parent_counts.items()}
    context["history_signatures"] = sorted({signature(row.expression, row.settings or {}) for row in rows
                                             if row.expression and row.validation_status == "VALID"})
    context["history_behavioral_signatures"] = sorted({behavioral_signature(row.expression, row.settings or {})
        for row in rows if row.expression and row.validation_status == "VALID"})
    context["unresolved_requests"] = sum(row.run_id == run.id and needs_reconciliation(row) for row in rows)
    if continuous:
        # Continuous PPA batches are independently audited before dispatch.
        # Keep the phase gate, but let the scheduler overlap the whole phase
        # instead of making the default phase a four-case serial batch.
        context["max_batch_cases"] = phase_limit
    # Unknown historical mechanism assignments are visible but not proposed as parents.
    context["seeds"] = [row for row in context["seeds"] if row["mechanism_id"] in base["mechanisms"]]
    context["finalists"] = [row for row in context["finalists"] if row["mechanism_id"] in base["mechanisms"]]
    if context["local_expansion"]:
        context["local_seeds"] = select_local_seeds(
            context["rows"], base,
            max_operators=context["strict_max_operator_count"],
            parent_total_counts=parent_counts,
            exhausted={tuple(item) for item in context.get("exhausted_templates", [])},
            limit=4 if continuous else 3)
    if (context["next_phase"] == "refinement" and not context["seeds"]
            and not context.get("local_seeds")):
        available_families = set(base["mechanisms"])
        if continuous and available_families and context["phase_counts"].get("discovery", 0) <= phase_limit - 4:
            context.update(next_phase="discovery", stop_reason=None)
        else:
            context.update(next_phase=None, stop_reason="PPA_NO_QUALIFIED_SEEDS")
    if context["next_phase"] == "confirmation" and not context["finalists"]:
        context.update(next_phase=None, stop_reason="PPA_NO_FINALISTS")
    if (continuous and context["next_phase"] != "confirmation"
            and context["phase_counts"].get("refinement", 0) == 0
            and context["phase_counts"].get("discovery", 0) <= phase_limit - 4):
        # Historical campaign rows remain available for seeds and deduplication,
        # but this run first fills its own complete control groups.
        context.update(next_phase="discovery", stop_reason=None)
    context["evidence_hash"] = digest({key: value for key, value in context.items() if key != "evidence_hash"})
    context["base"] = base
    return context


async def advance_epoch(db, task, run, *, expected_epoch):
    """Advance a paused, exhausted continuous batch exactly once."""
    if task.status != "PAUSED" or run.status != "PAUSED" or task.config.get("research_protocol") != "theme_ppa_v3":
        raise ValueError("PPA_EPOCH_REQUIRES_PAUSED_V3")
    if run.block_reason != "AWAITING_RESEARCH_BATCH":
        raise ValueError("PPA_EPOCH_REQUIRES_BATCH_BOUNDARY")
    await db.refresh(run, with_for_update=True)
    checkpoint = dict(run.checkpoint or {})
    current = int(checkpoint.get("ppa_epoch", 0))
    if current != expected_epoch:
        raise ValueError("PPA_STALE_EPOCH")
    batch = checkpoint.get("ppa_batch") or {}
    if checkpoint.get("ppa_batch_cursor", 0) < len(batch.get("cases") or []):
        raise ValueError("PPA_BATCH_STILL_ACTIVE")
    _, rows = await campaign_rows(db, task, run)
    if any(row.run_id == run.id and needs_reconciliation(row) for row in rows):
        raise ValueError("PPA_REQUEST_RECONCILIATION_PENDING")
    evidence = await feedback_context(db, task, run)
    if evidence["next_phase"] is not None:
        raise ValueError("PPA_EPOCH_NOT_EXHAUSTED")
    history = list(checkpoint.get("ppa_epoch_history") or [])
    history.append({"from": current, "to": current + 1, "reason": evidence["stop_reason"],
                    "at": datetime.now(timezone.utc).isoformat(), "phase_counts": evidence["phase_counts"]})
    run.checkpoint = {**checkpoint, "ppa_epoch": current + 1, "ppa_epoch_history": history,
                      "ppa_phase_limit": 16}
    await db.commit()
    return {"run_id": run.id, "epoch": current + 1, "previous_reason": evidence["stop_reason"]}


async def import_batch(db, task, run, batch):
    if task.status != "PAUSED" or run.status != "PAUSED" or task.config.get("research_protocol") not in {"theme_ppa_v2", "theme_ppa_v3"}:
        raise ValueError("PPA_BATCH_IMPORT_REQUIRES_PAUSED_V2")
    await db.refresh(run, with_for_update=True)
    checkpoint = dict(run.checkpoint or {})
    old = checkpoint.get("ppa_batch")
    if old and checkpoint.get("ppa_batch_cursor", 0) < len(old["cases"]):
        raise ValueError("PPA_BATCH_STILL_ACTIVE")
    evidence = await feedback_context(db, task, run)
    if batch.get("evidence_hash") != evidence["evidence_hash"]:
        raise ValueError("PPA_STALE_FEEDBACK")
    generation = batch.get("candidate_generation") or {}
    allowed_models = {"gpt-6-luna", "gpt-6-astra"} if task.config.get("research_protocol") == "theme_ppa_v3" else {"gpt-6-luna"}
    allowed_sources = {"cockpit_local_responses"}
    if evidence.get("local_expansion"):
        allowed_sources.update({"cockpit_seed_local_expansion", "measured_seed_local_expansion",
                                "measured_discovery_local_expansion"})
    model_valid = (generation.get("model") == "none" if generation.get("source") in {
        "measured_seed_local_expansion", "measured_discovery_local_expansion"}
                   else generation.get("model") in allowed_models)
    if (not model_valid or generation.get("source") not in allowed_sources
            or generation.get("plan_sha256") != digest({k: v for k, v in batch.items() if k != "candidate_generation"})):
        raise ValueError("PPA_BATCH_HASH_INVALID")
    checked = verify_batch_generation(task.config["ppa_manifest"], evidence, batch)
    if checked["cases"] != batch["cases"] or batch.get("base_hash") != digest(task.config["ppa_manifest"]):
        raise ValueError("PPA_BATCH_CONSTRAINT_MISMATCH")
    allocation = task.config["ppa_manifest"].get("discovery_allocation")
    if batch.get("phase") == "discovery" and allocation:
        groups = {case["pair_id"]: case["mechanism_id"] for case in checked["cases"]}
        if dict(Counter(groups.values())) != allocation:
            raise ValueError("PPA_DISCOVERY_ALLOCATION_MISMATCH")
    history = list(checkpoint.get("ppa_batch_history") or [])
    if old:
        history.append(old)
    run.checkpoint = {**checkpoint, "ppa_batch": batch, "ppa_batch_cursor": 0,
                      "ppa_batch_history": history, "ppa_stop_reason": None,
                      "ppa_execution_started_at": None, "ppa_batch_completed": [],
                      "ppa_phase_limit": 16 if task.config.get("research_protocol") == "theme_ppa_v3" else 12}
    run.block_reason = "PPA_BATCH_READY"
    await db.commit()
    return {"batch_hash": generation["plan_sha256"], "cases": len(batch["cases"]), "status": "PAUSED"}


async def pause(db, task, run, reason):
    from backend.services.ppa_progress import begin_pause
    await db.refresh(task, with_for_update=True)
    if task.status == "PAUSED":
        await db.refresh(run)
        if run.block_reason == "USER_PAUSED":
            return {"done": True, "reason": "USER_PAUSED"}
    task.status = run.status = "PAUSED"
    run.block_reason = reason
    run.checkpoint = {**begin_pause(run.checkpoint, reason, retry_at=run.next_retry_at), "ppa_stop_reason": reason}
    await db.commit()
    return {"done": True, "reason": reason}


async def persist_result(db, brain, task, run, case, settings, result, phase, quality_evaluator, fields):
    """Persist exactly one evaluated result, including recovered/cached results."""
    qualification_started = time.perf_counter()
    alpha_id = result.get("alpha_id")
    if not alpha_id:
        return None
    row = (await db.execute(select(ResearchCandidate).where(
        ResearchCandidate.run_id == run.id, ResearchCandidate.experiment_group == case["case_id"])
        .order_by(ResearchCandidate.id.desc()).limit(1).execution_options(populate_existing=True))).scalar_one()
    if (row.result_snapshot or {}).get("ppa_evaluation"):
        return row
    corr = {}
    if task.config.get("research_protocol") != "theme_ppa_v3":
        try:
            corr = await brain.check_power_pool_correlation(alpha_id)
        except Exception:
            corr = {}
    corr_value = corr.get("max") if isinstance(corr, dict) else None
    snapshot = safe_result(result)
    platform = evaluate_ppa_candidate(snapshot, task.config["ppa_manifest"], corr_value)
    final = platform if platform["status"] != "PPA_READY" else {
        **platform, "status": "PPA_PENDING", "reasons": ["ROBUSTNESS_REVIEW_PENDING"]}
    metrics = {**snapshot["metrics"], "checks": snapshot["checks"], "can_submit": snapshot["can_submit"],
               "stage": snapshot["stage"], "status": snapshot["status"]}
    strict = await quality_evaluator(task=task, expression=case["expression"], metrics=metrics,
                                     fields=fields, alpha_id=alpha_id)
    if row.request_status == "NOT_REQUESTED":
        row.request_status = "CACHED"
        row.validation_status = "VALID"
    row.wq_alpha_id, row.metrics, row.quality_status = alpha_id, snapshot["metrics"], strict
    row.gate_failures = metrics.get("_strict_gate_failures") or []
    row.result_snapshot = {**(row.result_snapshot or {}), **snapshot, "research_case": case,
                           "platform_ppa_evaluation": platform, "ppa_evaluation": final,
                           "power_pool_correlation": corr_value,
                           "qualification_seconds": round(time.perf_counter() - qualification_started, 3),
                           "evaluated_at": datetime.now(timezone.utc).isoformat()}
    saved = (await db.execute(select(Alpha.id).where(Alpha.alpha_id == alpha_id))).first()
    if not saved:
        db.add(Alpha(task_id=task.id, run_id=run.id, alpha_id=alpha_id, expression=case["expression"],
                     expression_hash=compute_expression_hash(case["expression"]), settings=settings,
                     dataset_id=case["dataset_id"], region=task.region, universe=task.universe,
                     delay=settings["delay"], decay=settings["decay"], neutralization=settings["neutralization"],
                     truncation=settings["truncation"], quality_status=strict, status="simulated",
                     is_sharpe=metrics.get("sharpe"), is_fitness=metrics.get("fitness"),
                     is_turnover=metrics.get("turnover"), is_margin=metrics.get("margin"),
                     metrics=metrics, is_metrics=metrics, checks=snapshot["checks"],
                     hypothesis=f"{case.get('mechanism_id')}: {case.get('expected_effect', 'confirmation')}"))
    db.add(TraceStep(task_id=task.id, run_id=run.id, step_type="PPA_V2_RESULT", step_order=row.id,
                     status="SUCCESS", output_data={"candidate_id": row.id, "ppa_status": final["status"]}))
    await db.commit()
    return row


async def reconcile_paused(db, task, run, brain, fields_by_dataset, quality_evaluator=None):
    """Poll one original request and complete its evaluation; never create a simulation."""
    if task.status != "PAUSED" or run.status != "PAUSED" or task.config.get("research_protocol") not in {"theme_ppa_v2", "theme_ppa_v3"}:
        raise ValueError("PPA_RECONCILE_REQUIRES_PAUSED_RUN")
    rows = (await db.execute(select(ResearchCandidate).where(ResearchCandidate.run_id == run.id)
                             .order_by(ResearchCandidate.id).execution_options(populate_existing=True))).scalars().all()
    candidates = [row for row in rows if needs_reconciliation(row)]
    if not candidates:
        return {"resolved": 0, "pending": 0}
    # A long-running original request must not permanently starve later ones.
    # Missing timestamps sort first so historical checkpoints need no migration.
    def last_attempt(candidate):
        value = (candidate.result_snapshot or {}).get("reconcile_last_attempt_at")
        return value if isinstance(value, str) else ""

    row = min(candidates, key=last_attempt)
    recovery_started = time.perf_counter()
    case = (row.result_snapshot or {}).get("research_case") or {}
    location = (row.result_snapshot or {}).get("request", {}).get("location")
    if row.request_status != "RESULT" and not location:
        raise ValueError("PPA_RECOVERY_LOCATION_MISSING")
    row.result_snapshot = {**(row.result_snapshot or {}),
                           "reconcile_last_attempt_at": datetime.now(timezone.utc).isoformat()}
    await db.commit()

    async def record_attempt(outcome):
        snapshot = row.result_snapshot or {}
        attempts = list(snapshot.get("reconcile_attempts") or [])
        attempts.append({"started_at": snapshot["reconcile_last_attempt_at"],
                         "duration_seconds": round(time.perf_counter() - recovery_started, 3),
                         "outcome": outcome})
        row.result_snapshot = {**snapshot, "reconcile_attempts": attempts}
        await db.commit()

    if row.request_status == "RESULT":
        result = row.result_snapshot or {}
    else:
        try:
            if (row.result_snapshot or {}).get("request", {}).get("transport") == "paired_multi":
                result = await asyncio.wait_for(
                    brain.poll_existing_multisim(location, row.expression),
                    timeout=PPA_MULTISIM_RECONCILE_TIMEOUT_SECONDS)
            else:
                result = await asyncio.wait_for(
                    brain.poll_existing_simulation(location),
                    timeout=PPA_RECONCILE_TIMEOUT_SECONDS,
                )
        except asyncio.TimeoutError:
            await record_attempt("TIMEOUT")
            return {"resolved": 0, "pending": len(candidates)}
    definitive_failure = str(result.get("status") or "").upper() in {"ERROR", "FAILED"}
    if definitive_failure and not result.get("error"):
        result = {**result, "error": "original simulation failed"}
    if not result.get("alpha_id") and ((not result.get("error") and not definitive_failure) or
                                        result.get("http_status") == 429 or
                                        isinstance(result.get("http_status"), int) and result["http_status"] >= 500):
        result = {**result, "error": "uncertain original simulation poll"}
    await record_attempt("RESULT" if result.get("alpha_id") else
                         "PENDING" if is_uncertain_result(result) else "FAILED")
    if result.get("alpha_id"):
        dataset = case.get("dataset_id")
        if not case.get("case_id") or not fields_by_dataset.get(dataset):
            raise ValueError("PPA_RECOVERY_CASE_OR_FIELDS_MISSING")
    if row.request_status != "RESULT":
        ledger = ResearchLedger(task.id, run.id,
                                None if task.config.get("research_protocol") == "theme_ppa_v3" else 80,
                                budget_root_run_id=task.config["budget_root_run_id"])
        await ledger.finish(row.id, result)
        await db.refresh(row)
    if result.get("alpha_id"):
        if quality_evaluator is None:
            from backend.agents.mining_agent import MiningAgent
            quality_evaluator = MiningAgent(db, brain)._quality_status_for_optimization_result
        await persist_result(db, brain, task, run, case, row.settings, result,
                             row.source.lower(), quality_evaluator, fields_by_dataset[case["dataset_id"]])
    await db.refresh(row)
    if result.get("alpha_id"):
        row.result_snapshot = {**(row.result_snapshot or {}), "recovery": {
            "source": "original_location", "duration_seconds": round(time.perf_counter() - recovery_started, 3),
            "observed_at": datetime.now(timezone.utc).isoformat()}}
        await db.commit()
    resolved = int(row.request_status in {"RESULT", "FAILED"}
                   and bool((row.result_snapshot or {}).get("ppa_evaluation") or
                            row.request_status == "FAILED"))
    remaining = len(candidates) - resolved
    if not remaining and run.block_reason == "PPA_REQUEST_RECONCILIATION_PENDING":
        batch_cases = ((run.checkpoint or {}).get("ppa_batch") or {}).get("cases") or []
        current = {item.experiment_group: item for item in rows}
        unfinished = any(
            not current.get(case["case_id"])
            or needs_reconciliation(current[case["case_id"]])
            or current[case["case_id"]].request_status not in {"RESULT", "CACHED", "FAILED"}
            for case in batch_cases
        )
        needs_batch_close = (run.checkpoint or {}).get("ppa_batch_cursor", 0) < len(batch_cases)
        run.block_reason = ("PPA_BATCH_READY" if unfinished or needs_batch_close
                            else "AWAITING_RESEARCH_BATCH")
        await db.commit()
    return {"resolved": resolved, "pending": remaining}


async def reconcile_qualification(db, task, run, brain, limit=1):
    """Refresh existing Alpha evidence with GETs; never resimulate or submit."""
    if task.config.get("research_protocol") != "theme_ppa_v3" or run.task_id != task.id:
        raise ValueError("PPA_QUALIFICATION_SCOPE_INVALID")
    if not 1 <= limit <= 20:
        raise ValueError("PPA_QUALIFICATION_LIMIT_INVALID")
    rows = (await db.execute(select(ResearchCandidate).where(
        ResearchCandidate.run_id == run.id, ResearchCandidate.request_status.in_(("RESULT", "CACHED")),
        ResearchCandidate.wq_alpha_id.is_not(None),
    ).order_by(ResearchCandidate.id.desc()).execution_options(populate_existing=True))).scalars().all()
    now = datetime.now(timezone.utc)
    shared_retry = (run.checkpoint or {}).get("ppa_qualification_retry_at")
    try:
        check_throttled = bool(shared_retry and datetime.fromisoformat(shared_retry) > now)
    except (TypeError, ValueError):
        check_throttled = False
    def due(row):
        retry = ((row.result_snapshot or {}).get("qualification_check") or {}).get("next_retry_at")
        if not retry:
            return True
        try:
            return datetime.fromisoformat(retry) <= now
        except (TypeError, ValueError):
            return True
    pending = [row for row in rows
               if (row.result_snapshot or {}).get("platform_ppa_evaluation", {}).get("status") == "PPA_PENDING"
               and due(row)]
    selected = [] if check_throttled else pending[:limit]
    async def fetch(row):
        alpha_id = row.wq_alpha_id
        old_corr = (row.result_snapshot or {}).get("power_pool_correlation")
        calls = [brain.read_pre_submit_checks(alpha_id, max_wait=10),
                 brain.get_alpha_evidence(alpha_id)]
        if old_corr is None:
            calls.append(brain.check_power_pool_correlation(alpha_id))
        try:
            values = await asyncio.wait_for(asyncio.gather(*calls, return_exceptions=True), timeout=25)
        except asyncio.TimeoutError:
            return {"status": "PENDING", "checks": []}, {}, {}
        checked = values[0] if isinstance(values[0], dict) else {"status": "ERROR", "checks": []}
        details = values[1] if isinstance(values[1], dict) else {}
        corr = values[2] if len(values) > 2 and isinstance(values[2], dict) else {}
        return checked, details, corr

    observations = await asyncio.gather(*(fetch(row) for row in selected))
    for row, (checked, details, corr) in zip(selected, observations):
        alpha_id = row.wq_alpha_id
        if checked.get("http_status") in {401, 403}:
            raise ValueError("PPA_QUALIFICATION_AUTH_FAILED")
        old = row.result_snapshot or {}
        updated = old
        if details.get("success") and details.get("alpha_id") == alpha_id:
            refreshed = safe_result({**old, **details,
                                     "metrics": {**(old.get("metrics") or {}),
                                                 **(details.get("metrics") or {})}})
            updated = {**old, **refreshed,
                       "metrics": {**(old.get("metrics") or {}), **refreshed["metrics"]}}
        if checked.get("status") == "COMPLETE":
            sanitized = safe_result({**updated, "checks": checked["checks"]})
            updated = {**updated, "checks": sanitized["checks"]}
        corr_value = old.get("power_pool_correlation")
        if corr_value is None:
            corr_value = corr.get("max") if isinstance(corr, dict) else None
        platform = evaluate_ppa_candidate(updated, task.config["ppa_manifest"], corr_value)
        final = (platform if platform["status"] != "PPA_READY" else
                 {**platform, "status": "PPA_PENDING", "reasons": ["ROBUSTNESS_REVIEW_PENDING"]})
        retry_seconds = max(60.0, min(3600.0, float(checked.get("retry_after") or 60)))
        if checked.get("status") == "THROTTLED":
            checkpoint = dict(run.checkpoint or {})
            checkpoint["ppa_qualification_retry_at"] = (now + timedelta(seconds=retry_seconds)).isoformat()
            run.checkpoint = checkpoint
        next_retry = ((now + timedelta(seconds=retry_seconds if checked.get("status") == "THROTTLED" else 300)).isoformat()
                      if platform["status"] == "PPA_PENDING" else None)
        row.metrics = updated.get("metrics") or {}
        row.result_snapshot = {**updated, "platform_ppa_evaluation": platform,
                               "ppa_evaluation": final, "power_pool_correlation": corr_value,
                               "qualification_check": {"source": "WQ_ALPHA_CHECK",
                                   "status": checked.get("status"), "observed_at": now.isoformat(),
                                   "http_status": checked.get("http_status"),
                                   "retry_after": checked.get("retry_after"),
                                   "next_retry_at": next_retry}}
        await db.commit()
    batch = (run.checkpoint or {}).get("ppa_batch") or {}
    if (selected and batch.get("phase") == "confirmation"
            and (run.checkpoint or {}).get("ppa_batch_cursor", 0) >= len(batch.get("cases") or [])):
        await finish_confirmation(db, task, run, batch)
    # A previously paused run may proceed even if individual Alphas remain pending.
    if run.status == "PAUSED" and run.block_reason == "PPA_QUALIFICATION_EVIDENCE_PENDING":
        from backend.services.ppa_progress import begin_pause
        batch = ((run.checkpoint or {}).get("ppa_batch") or {}).get("cases") or []
        reason = ("AWAITING_RESEARCH_BATCH" if (run.checkpoint or {}).get("ppa_batch_cursor", 0) >= len(batch)
                  else "PPA_BATCH_READY")
        run.block_reason = reason
        calibrated = dict(run.checkpoint or {})
        calibrated.pop("ppa_transport", None)
        if "ppa_calibration_id" not in calibrated:
            calibrated["ppa_calibration_id"] = now.isoformat()
            calibrated["ppa_max_in_flight"] = 8
        run.checkpoint = begin_pause(calibrated, reason)
        await db.commit()
    return {"reviewed": len(selected), "pending": sum(
        (row.result_snapshot or {}).get("platform_ppa_evaluation", {}).get("status") == "PPA_PENDING"
        for row in rows), "run_block_reason": run.block_reason}


async def finish_confirmation(db, task, run, batch):
    _, rows = await campaign_rows(db, task, run)
    by_case = {row.experiment_group: row for row in rows if row.request_status in {"RESULT", "CACHED"}}
    for parent_id in {case.get("parent_candidate_id") for case in batch["cases"]}:
        parent = next((row for row in rows if row.id == parent_id), None)
        if not parent or (parent.result_snapshot or {}).get("platform_ppa_evaluation", {}).get("status") != "PPA_READY":
            continue
        cases = [case for case in batch["cases"] if case.get("parent_candidate_id") == parent_id]
        children = [by_case.get(case["case_id"]) for case in cases]
        if len(cases) != 2 or any(not child or (child.result_snapshot or {}).get("platform_ppa_evaluation", {}).get("status") != "PPA_READY"
                                 or not seed_decision(research_row(child))["eligible"] for child in children):
            continue
        # A successor's verdict is recorded on its own run, never rewrite a historical run's verdict.
        reviews = dict((run.checkpoint or {}).get("ppa_robustness_reviews") or {})
        reviews[str(parent_id)] = {"status": "PASS", "confirmation_ids": [child.id for child in children]}
        run.checkpoint = {**(run.checkpoint or {}), "ppa_robustness_reviews": reviews, "ppa_stop_reason": "PPA_READY"}
        if parent.run_id == run.id:
            parent.result_snapshot = {**parent.result_snapshot, "ppa_evaluation": parent.result_snapshot["platform_ppa_evaluation"],
                                      "robustness_review": reviews[str(parent_id)]}
        else:
            # Historical evidence is immutable; the successor owns its review verdict.
            db.add(ResearchCandidate(task_id=task.id, run_id=run.id, source="REVIEW",
                family_id=f"theme_ppa_v2:{task.config['ppa_manifest']['theme_id']}",
                experiment_group=f"REVIEW_{parent_id}", parent_candidate_id=parent_id,
                expression=parent.expression, expression_hash=parent.expression_hash,
                settings_hash=parent.settings_hash, settings=parent.settings,
                validation_status="VALID", request_status="CACHED", wq_alpha_id=parent.wq_alpha_id,
                quality_status=parent.quality_status, gate_failures=parent.gate_failures or [],
                metrics=parent.metrics or {}, result_snapshot={**(parent.result_snapshot or {}),
                    "ppa_evaluation": parent.result_snapshot["platform_ppa_evaluation"],
                    "robustness_review": reviews[str(parent_id)]}))
        task.status = run.status = "COMPLETED"
        task.progress_current = 1
        run.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.commit()
        return True
    return False


async def run_parallel_batch(db, brain, task, run, batch, base, fields_by_dataset,
                             quality_evaluator, ledger):
    """Overlap independent WQ requests; commit each result as it arrives."""
    from backend.alpha_semantic_validator import AlphaSemanticValidator

    cases = batch["cases"]
    checkpoint = run.checkpoint or {}
    existing = (await db.execute(select(ResearchCandidate).where(
        ResearchCandidate.run_id == run.id,
        ResearchCandidate.experiment_group.in_([case["case_id"] for case in cases]),
    ))).scalars().all()
    finished = {row.experiment_group for row in existing if
                row.request_status == "FAILED" and (row.result_snapshot or {}).get("http_status") != 429 or
                row.request_status in {"RESULT", "CACHED"} and (row.result_snapshot or {}).get("ppa_evaluation")}
    first_review = (batch["phase"] == "discovery" and not checkpoint.get("ppa_first_batch_reviewed"))
    pending = []
    for index, case in enumerate(cases):
        if case["case_id"] in finished:
            continue
        validated = AlphaSemanticValidator(
            fields=fields_by_dataset[case["dataset_id"]], strict_field_check=True,
        ).validate(case["expression"])
        if not validated.valid:
            return await pause(db, task, run, "PPA_CASE_INVALID")
        pending.append((index, case))
    if pending and not checkpoint.get("ppa_execution_started_at"):
        run.checkpoint = {**checkpoint, "ppa_execution_started_at": datetime.now(timezone.utc).isoformat()}
        await db.commit()
    from backend.services.ppa_quota import next_parallelism
    concurrency = next_parallelism(checkpoint.get("ppa_max_in_flight", 2),
                                   checkpoint.get("ppa_throughput_history") or [])
    if checkpoint.get("ppa_parallel_scheduler") != "v1":
        # Preserve safe saved limits while migrating older checkpoints.
        concurrency = min(4, max(2, int(checkpoint.get("ppa_max_in_flight", 2))))
        run.checkpoint = {**(run.checkpoint or {}),
                          "ppa_max_in_flight": concurrency,
                          "ppa_parallel_scheduler": "v1"}
        run.checkpoint.pop("ppa_transport", None)
        await db.commit()
    checkpoint = run.checkpoint or {}
    logger.info("ppa_batch path=parallel concurrency=%d cases=%d", concurrency, len(cases),
                extra={"ppa_path": "parallel", "concurrency": concurrency, "cases": len(cases)})
    semaphore = asyncio.Semaphore(concurrency)
    stop_dispatch = asyncio.Event()
    quota_exhausted = asyncio.Event()
    retry_wait = 0.0
    active_parents = 0
    parent_peak = 0
    # Concurrent requests must not read ORM attributes while the owner session
    # refreshes them to merge a manual pause.
    batch_epoch = int((run.checkpoint or {}).get("ppa_epoch", 0))
    batch_region, batch_universe = task.region, task.universe

    def review_result(result):
        nonlocal retry_wait
        if result.get("http_status") == 429 or result.get("error") in {
            "BUDGET_EXHAUSTED", "TASK_NOT_RUNNING",
        } or is_uncertain_result(result):
            stop_dispatch.set()
            try:
                retry_wait = max(retry_wait, float(result.get("retry_after") or 0))
            except (TypeError, ValueError):
                pass
        quota = getattr(ledger, "quota_window", None)
        if quota is not None and isinstance(result.get("rate_limit"), dict):
            quota.observe(result["rate_limit"])
            if quota.remaining == 0:
                quota_exhausted.set()
                stop_dispatch.set()

    def prepare_case(original):
        case = {**original, "epoch": batch_epoch}
        settings = {**base["settings"], **case.get("settings", {}), "region": batch_region,
                    "universe": batch_universe, "delay": base["delay"]}
        return case, settings

    async def simulate_one(index, original):
        if stop_dispatch.is_set():
            return index, original, None, None, None
        case, settings = prepare_case(original)
        wrapped = ResearchBudgetBrain(brain, ledger)
        wrapped.register(case["expression"], group=case["case_id"], arm=case.get("arm"),
                         pair_id=case.get("pair_id"), parent_candidate_id=case.get("parent_candidate_id"),
                         research_case=case)
        quota = getattr(ledger, "quota_window", None)
        result = await ledger.recover(case["expression"], settings)
        if not result:
            if quota is not None and not quota.claim():
                quota_exhausted.set()
                stop_dispatch.set()
                return index, case, settings, None, wrapped
            with wrapped.source(batch["phase"].upper()):
                result = await wrapped.simulate_alpha(case["expression"], **settings)
        else:
            await ledger.reserve(case["expression"], settings, batch["phase"].upper(),
                                 wrapped.metadata[case["expression"]])
        review_result(result)
        return index, case, settings, result, wrapped

    async def simulate_pending(index, original):
        nonlocal active_parents, parent_peak
        async with semaphore:
            if stop_dispatch.is_set():
                return []
            active_parents += 1
            parent_peak = max(parent_peak, active_parents)
            try:
                return [await simulate_one(index, original)]
            finally:
                active_parents -= 1

    running = [asyncio.create_task(simulate_pending(*item)) for item in pending]
    block_reason = None
    execution_error_type = None
    try:
        for completed in asyncio.as_completed(running):
            try:
                outcomes = await completed
            except Exception as exc:
                stop_dispatch.set()
                block_reason = "PPA_EXECUTION_ERROR"
                execution_error_type = type(exc).__name__
                continue
            for index, case, settings, result, wrapped in outcomes:
                if result is None:
                    continue
                row = await persist_result(db, wrapped, task, run, case, settings, result, batch["phase"],
                                           quality_evaluator, fields_by_dataset[case["dataset_id"]])
                # A concurrent manual pause updates the same checkpoint. Refresh
                # under the task/run locks before merging the completed case.
                await db.refresh(task, with_for_update=True)
                await db.refresh(run, with_for_update=True)
                if task.status != "RUNNING":
                    stop_dispatch.set()
                if result.get("http_status") != 429 and not is_uncertain_result(result):
                    finished.add(case["case_id"])
                done_indices = {i for i, item in enumerate(cases) if item["case_id"] in finished}
                cursor = next((i for i in range(len(cases)) if i not in done_indices), len(cases))
                run.checkpoint = {**(run.checkpoint or {}), "ppa_batch_cursor": cursor,
                                  "ppa_batch_completed": sorted(done_indices)}
                await db.commit()
                if result.get("http_status") == 429:
                    block_reason = "PPA_RATE_LIMIT"
                elif is_uncertain_result(result):
                    block_reason = "PPA_REQUEST_RECONCILIATION_PENDING"
    finally:
        if running:
            await asyncio.gather(*running, return_exceptions=True)
    await db.refresh(task, with_for_update=True)
    await db.refresh(run, with_for_update=True)
    if task.status == "PAUSED" and run.block_reason == "USER_PAUSED":
        await db.commit()
        return {"done": True, "reason": "USER_PAUSED"}
    if quota_exhausted.is_set():
        block_reason = block_reason or "PPA_RATE_LIMIT"
    if block_reason:
        if execution_error_type:
            run.checkpoint = {**(run.checkpoint or {}), "ppa_execution_error": execution_error_type}
        history = list((run.checkpoint or {}).get("ppa_throughput_history") or [])[-19:]
        history.append({"batch_hash": (batch.get("candidate_generation") or {}).get("plan_sha256"),
                        "concurrency": concurrency, "completed": len(finished), "cases": len(cases),
                        "parent_peak": parent_peak, "supply_limited": len(pending) < concurrency,
                        "rate_limited": block_reason == "PPA_RATE_LIMIT",
                        "unknown": int(block_reason == "PPA_REQUEST_RECONCILIATION_PENDING"),
                        "execution_error": block_reason == "PPA_EXECUTION_ERROR"})
        run.checkpoint = {**(run.checkpoint or {}), "ppa_throughput_history": history}
        if block_reason == "PPA_RATE_LIMIT":
            from backend.services.ppa_quota import retry_delay_for_429

            quota = getattr(ledger, "quota_window", None)
            exhausted = quota is not None and quota.observed_remaining == 0
            streak = int((run.checkpoint or {}).get("ppa_429_streak", 0)) + 1
            wait_seconds = retry_delay_for_429(quota, retry_after=retry_wait, streak=streak)
            run.checkpoint = {**(run.checkpoint or {}), "ppa_429_streak": streak,
                              "ppa_max_in_flight": max(2, concurrency // 2),
                              "ppa_rate_limit_kind": "DAILY" if exhausted else "BURST"}
            run.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)
        elif block_reason == "PPA_REQUEST_RECONCILIATION_PENDING":
            run.checkpoint = {**(run.checkpoint or {}),
                              "ppa_max_in_flight": max(2, concurrency // 2)}
        return await pause(db, task, run, block_reason)
    review_due = first_review and all(case["case_id"] in finished for case in cases)
    if len(finished) >= len(cases):
        # A recovered final request may leave the cursor behind even though all
        # original Alpha results are persisted. Close the batch before importing another.
        run.checkpoint = {**(run.checkpoint or {}), "ppa_batch_cursor": len(cases),
                          "ppa_batch_completed": list(range(len(cases)))}
        await db.commit()
        if batch["phase"] == "confirmation" and await finish_confirmation(db, task, run, batch):
            return {"done": True, "reason": "PPA_READY"}
        rows = (await db.execute(select(ResearchCandidate).where(
            ResearchCandidate.run_id == run.id,
            ResearchCandidate.experiment_group.in_([case["case_id"] for case in cases]),
        ))).scalars().all()
        completed_count = sum(row.request_status == "RESULT" for row in rows)
        # A resolved original request still means this batch encountered an
        # uncertainty and must not count toward the two-clean-batch ramp.
        unknown_count = sum(row.request_status == "UNKNOWN" or bool((row.result_snapshot or {}).get("recovery"))
                            for row in rows)
        from backend.services.ppa_progress import batch_request_timing
        timing = batch_request_timing(rows)
        # Pauses and qualification checks are outside WQ's active request wall time.
        wall_seconds = timing["wall_seconds"]
        history = list((run.checkpoint or {}).get("ppa_throughput_history") or [])[-19:]
        batch_hash = (batch.get("candidate_generation") or {}).get("plan_sha256")
        prior = [item for item in history if batch_hash and item.get("batch_hash") == batch_hash]
        if not any(item.get("batch_complete") for item in prior):
            history.append({"batch_hash": batch_hash, "batch_complete": True,
                            "concurrency": concurrency, "completed": completed_count, "cases": len(cases),
                            "wall_seconds": round(wall_seconds, 3) if wall_seconds is not None else None,
                            "request_seconds": round(timing["request_seconds"], 3)
                            if timing["request_seconds"] is not None else None,
                            "timed_requests_included": timing["included"],
                            "timed_requests_excluded": timing["excluded"],
                            "unknown": unknown_count or any(item.get("unknown") for item in prior),
                            "rate_limited": any(item.get("rate_limited") for item in prior),
                            "execution_error": any(item.get("execution_error") for item in prior),
                            "parent_peak": parent_peak, "parent_groups": len(pending),
                            "supply_limited": len(pending) < concurrency})
        run.checkpoint = {**(run.checkpoint or {}), "ppa_throughput_history": history,
                          "ppa_max_in_flight": concurrency, "ppa_429_streak": 0}
        if review_due:
            run.checkpoint = {**run.checkpoint, "ppa_first_batch_reviewed": True}
            await db.commit()
            return await pause(db, task, run, "PPA_FIRST_BATCH_REVIEW")
        await db.commit()
        context = await feedback_context(db, task, run)
        if context["next_phase"] is None:
            run.checkpoint = {**run.checkpoint, "ppa_epoch": int(run.checkpoint.get("ppa_epoch", 0)) + 1}
        return await pause(db, task, run, "AWAITING_RESEARCH_BATCH")
    return {"done": False, "cursor": run.checkpoint.get("ppa_batch_cursor", 0)}


async def run_theme_v2(db, brain, task, run, fields_by_dataset, quality_evaluator=None):
    continuous = task.config.get("research_protocol") == "theme_ppa_v3"
    if task.status != "RUNNING":
        return {"done": True, "reason": task.status}
    from backend.services.theme_ppa import validate_theme_task, _timestamp
    reason = validate_theme_task(task)
    if reason:
        return await pause(db, task, run, reason)
    base = task.config["ppa_manifest"]
    if continuous:
        root = await db.get(ExperimentRun, task.config["budget_root_run_id"])
        root_config = (((root.config_snapshot or {}).get("task") or {}).get("config") or {}) if root else {}
        historical = root_config.get("ppa_manifest") or {}
        if not root or not new_fields_have_evidence(base, historical):
            return await pause(db, task, run, "PPA_FIELD_EVIDENCE_REQUIRED")
    now = datetime.now(timezone.utc)
    if not _timestamp(base["starts_at"]) <= now <= _timestamp(base["ends_at"]):
        return await pause(db, task, run, "PPA_THEME_NOT_ACTIVE")
    batch = (run.checkpoint or {}).get("ppa_batch")
    if not batch:
        return await pause(db, task, run, "AWAITING_RESEARCH_BATCH")
    if batch.get("base_hash") != digest(base) or batch["candidate_generation"]["plan_sha256"] != digest({k: v for k, v in batch.items() if k != "candidate_generation"}):
        return await pause(db, task, run, "PPA_BATCH_HASH_INVALID")
    if any(not fields_by_dataset.get(key) for key in base["datasets"]):
        return await pause(db, task, run, "PPA_FIELDS_NOT_AVAILABLE")
    if not runtime_fields_match(base, fields_by_dataset):
        return await pause(db, task, run, "PPA_FIELD_EVIDENCE_MISMATCH")
    ledger = ResearchLedger(task.id, run.id, None if continuous else 80,
                            f"theme_ppa_v3:{base['theme_id']}" if continuous else f"theme_ppa_v2:{base['theme_id']}",
                            budget_root_run_id=task.config["budget_root_run_id"])
    pending = (await db.execute(select(ResearchCandidate).where(
        ResearchCandidate.run_id == run.id,
        ResearchCandidate.request_status.in_(("RESERVED", "REQUESTED", "UNKNOWN", "RESULT", "FAILED"))
    ))).scalars().all()
    if any(needs_reconciliation(row) for row in pending):
        return await pause(db, task, run, "PPA_REQUEST_RECONCILIATION_PENDING")
    if continuous:
        from backend.services.ppa_quota import restore_quota
        ledger.quota_window = restore_quota([(row.result_snapshot or {}) for row in pending])
    wrapped = ResearchBudgetBrain(brain, ledger)
    if quality_evaluator is None:
        from backend.agents.mining_agent import MiningAgent
        quality_evaluator = MiningAgent(db, wrapped)._quality_status_for_optimization_result
    start = int((run.checkpoint or {}).get("ppa_batch_cursor", 0))
    if continuous:
        return await run_parallel_batch(db, brain, task, run, batch, base, fields_by_dataset,
                                        quality_evaluator, ledger)
    for index in range(start, min(start + 4, len(batch["cases"]))):
        await db.refresh(task)
        if task.status != "RUNNING":
            return {"done": True, "reason": task.status}
        case = batch["cases"][index]
        settings = {**base["settings"], **case.get("settings", {}), "region": task.region,
                    "universe": task.universe, "delay": base["delay"]}
        from backend.alpha_semantic_validator import AlphaSemanticValidator
        validated = AlphaSemanticValidator(fields=fields_by_dataset[case["dataset_id"]], strict_field_check=True).validate(case["expression"])
        if not validated.valid:
            return await pause(db, task, run, "PPA_CASE_INVALID")
        wrapped.register(case["expression"], group=case["case_id"], arm=case.get("arm"),
                         pair_id=case.get("pair_id"), parent_candidate_id=case.get("parent_candidate_id"), research_case=case)
        result = await ledger.recover(case["expression"], settings)
        if not result:
            with wrapped.source(batch["phase"].upper()):
                result = await wrapped.simulate_alpha(case["expression"], **settings)
        else:
            # Reserve creates a noncharged reuse record only if this case has no prior record.
            existing = (await db.execute(select(ResearchCandidate.id).where(ResearchCandidate.run_id == run.id,
                                        ResearchCandidate.experiment_group == case["case_id"]))).first()
            if not existing:
                await ledger.reserve(case["expression"], settings, batch["phase"].upper(), wrapped.metadata[case["expression"]])
        row = await persist_result(db, wrapped, task, run, case, settings, result, batch["phase"], quality_evaluator,
                                   fields_by_dataset[case["dataset_id"]])
        run.checkpoint = {**(run.checkpoint or {}), "ppa_batch_cursor": index + 1}
        await db.commit()
        if row and batch["phase"] == "confirmation" and await finish_confirmation(db, task, run, batch):
            return {"done": True, "reason": "PPA_READY"}
        if result.get("http_status") == 429 or (not result.get("alpha_id") and result.get("error") == "BUDGET_EXHAUSTED"):
            reset_at = getattr(ledger.quota_window, "reset_at", None)
            wait_seconds = (max(0, reset_at - time.monotonic()) if reset_at is not None
                            else max(0, float(result.get("retry_after") or 0)))
            if wait_seconds:
                run.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)
            return await pause(db, task, run, "PPA_RATE_LIMIT")
        if not result.get("alpha_id") and result.get("error") in {"DUPLICATE", "TASK_NOT_RUNNING"}:
            return await pause(db, task, run, result["error"])
        if row and (row.result_snapshot or {}).get("platform_ppa_evaluation", {}).get("status") == "PPA_PENDING" and (evidence_metrics(research_row(row)).get("amer_sharpe") is None or evidence_metrics(research_row(row))["amer_sharpe"] > 1):
            return await pause(db, task, run, "PPA_QUALIFICATION_EVIDENCE_PENDING")
        if (batch["phase"] == "discovery" and index + 1 == 4
                and not (run.checkpoint or {}).get("ppa_first_batch_reviewed")):
            run.checkpoint = {**(run.checkpoint or {}), "ppa_first_batch_reviewed": True}
            return await pause(db, task, run, "PPA_FIRST_BATCH_REVIEW")
    if run.checkpoint["ppa_batch_cursor"] >= len(batch["cases"]):
        pending = (await db.execute(select(ResearchCandidate).where(
            ResearchCandidate.run_id == run.id,
            ResearchCandidate.request_status.in_(("RESERVED", "REQUESTED", "UNKNOWN", "RESULT"))
        ))).scalars().all()
        if any(needs_reconciliation(row) for row in pending):
            return await pause(db, task, run, "PPA_REQUEST_RECONCILIATION_PENDING")
        context = await feedback_context(db, task, run)
        if context["next_phase"] is None:
            task.status = run.status = "COMPLETED"
            run.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
            run.checkpoint = {**run.checkpoint, "ppa_stop_reason": context["stop_reason"]}
            await db.commit()
            return {"done": True, "reason": context["stop_reason"]}
        return await pause(db, task, run, "AWAITING_RESEARCH_BATCH")
    return {"done": False, "cursor": run.checkpoint["ppa_batch_cursor"]}
