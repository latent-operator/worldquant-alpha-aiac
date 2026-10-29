"""Stage-frozen feedback protocol with campaign accounting and per-result commits."""
from collections import Counter
from datetime import datetime, timezone
from sqlalchemy import select

from backend.models import MiningTask, ExperimentRun, ResearchCandidate, Alpha, TraceStep
from backend.alpha_semantic_validator import compute_expression_hash
from backend.services.ppa_feedback import build_feedback, digest, freeze_batch, research_row
from backend.services.ppa_eligibility import evaluate_ppa_candidate
from backend.services.ppa_research_policy import evidence_metrics, seed_decision
from backend.services.research_audit import ResearchLedger, ResearchBudgetBrain, safe_result

CHARGED = {"RESERVED", "REQUESTED", "UNKNOWN", "FAILED", "RESULT"}


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
    identities = [(m["dataset_id"], m["hypothesis"].strip().lower(), m["direction"].lower()) for m in mechanisms.values()]
    if len(set(identities)) != len(identities):
        return "PPA_MECHANISM_ALIAS"
    if any(name not in mechanisms for name in (base.get("historical_mechanisms") or {}).values()):
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
    phase_counts = dict(Counter(row.source.lower() for row in rows
                                if row.run_id != root.id and row.request_status in CHARGED))
    remaining = max(0, 80 - sum(row.request_status in CHARGED for row in rows))
    context = build_feedback(converted, remaining, phase_counts)
    # Unknown historical mechanism assignments are visible but not proposed as parents.
    context["seeds"] = [row for row in context["seeds"] if row["mechanism_id"] in base["mechanisms"]]
    context["finalists"] = [row for row in context["finalists"] if row["mechanism_id"] in base["mechanisms"]]
    if context["next_phase"] == "refinement" and not context["seeds"]:
        context.update(next_phase=None, stop_reason="PPA_NO_QUALIFIED_SEEDS")
    if context["next_phase"] == "confirmation" and not context["finalists"]:
        context.update(next_phase=None, stop_reason="PPA_NO_FINALISTS")
    context["evidence_hash"] = digest({key: value for key, value in context.items() if key != "evidence_hash"})
    context["base"] = base
    return context


async def import_batch(db, task, run, batch):
    if task.status != "PAUSED" or run.status != "PAUSED" or task.config.get("research_protocol") != "theme_ppa_v2":
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
    if (generation.get("model") != "gpt-6-luna" or generation.get("source") != "cockpit_local_responses"
            or generation.get("plan_sha256") != digest({k: v for k, v in batch.items() if k != "candidate_generation"})):
        raise ValueError("PPA_BATCH_HASH_INVALID")
    checked = freeze_batch(task.config["ppa_manifest"], evidence, batch)
    if checked["cases"] != batch["cases"] or batch.get("base_hash") != digest(task.config["ppa_manifest"]):
        raise ValueError("PPA_BATCH_CONSTRAINT_MISMATCH")
    history = list(checkpoint.get("ppa_batch_history") or [])
    if old:
        history.append(old)
    run.checkpoint = {**checkpoint, "ppa_batch": batch, "ppa_batch_cursor": 0,
                      "ppa_batch_history": history, "ppa_stop_reason": None}
    run.block_reason = "PPA_BATCH_READY"
    await db.commit()
    return {"batch_hash": generation["plan_sha256"], "cases": len(batch["cases"]), "status": "PAUSED"}


async def pause(db, task, run, reason):
    task.status = run.status = "PAUSED"
    run.block_reason = reason
    run.checkpoint = {**(run.checkpoint or {}), "ppa_stop_reason": reason}
    await db.commit()
    return {"done": True, "reason": reason}


async def persist_result(db, brain, task, run, case, settings, result, phase, quality_evaluator, fields):
    """Persist exactly one evaluated result, including recovered/cached results."""
    alpha_id = result.get("alpha_id")
    if not alpha_id:
        return None
    row = (await db.execute(select(ResearchCandidate).where(
        ResearchCandidate.run_id == run.id, ResearchCandidate.experiment_group == case["case_id"])
        .order_by(ResearchCandidate.id.desc()).limit(1).execution_options(populate_existing=True))).scalar_one()
    if (row.result_snapshot or {}).get("ppa_evaluation"):
        return row
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
                           "evaluated_at": datetime.now(timezone.utc).isoformat()}
    saved = (await db.execute(select(Alpha.id).where(Alpha.run_id == run.id, Alpha.alpha_id == alpha_id))).first()
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


async def run_theme_v2(db, brain, task, run, fields_by_dataset, quality_evaluator=None):
    if task.status != "RUNNING":
        return {"done": True, "reason": task.status}
    from backend.services.theme_ppa import validate_theme_task, _timestamp
    reason = validate_theme_task(task)
    if reason:
        return await pause(db, task, run, reason)
    base = task.config["ppa_manifest"]
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
    ledger = ResearchLedger(task.id, run.id, 80, f"theme_ppa_v2:{base['theme_id']}",
                            budget_root_run_id=task.config["budget_root_run_id"])
    if hasattr(brain, "poll_existing_simulation"):
        await ledger.reconcile(brain.poll_existing_simulation)
    wrapped = ResearchBudgetBrain(brain, ledger)
    if quality_evaluator is None:
        from backend.agents.mining_agent import MiningAgent
        quality_evaluator = MiningAgent(db, wrapped)._quality_status_for_optimization_result
    start = int((run.checkpoint or {}).get("ppa_batch_cursor", 0))
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
        if result.get("http_status") == 429 or (not result.get("alpha_id") and result.get("error") in {"BUDGET_EXHAUSTED", "DUPLICATE", "TASK_NOT_RUNNING"}):
            return await pause(db, task, run, result.get("error") or "PPA_RATE_LIMIT")
        if row and (row.result_snapshot or {}).get("platform_ppa_evaluation", {}).get("status") == "PPA_PENDING" and (evidence_metrics(research_row(row)).get("amer_sharpe") is None or evidence_metrics(research_row(row))["amer_sharpe"] > 1):
            return await pause(db, task, run, "PPA_QUALIFICATION_EVIDENCE_PENDING")
        if (batch["phase"] == "discovery" and index + 1 == 4
                and not (run.checkpoint or {}).get("ppa_first_batch_reviewed")):
            run.checkpoint = {**(run.checkpoint or {}), "ppa_first_batch_reviewed": True}
            return await pause(db, task, run, "PPA_FIRST_BATCH_REVIEW")
    if run.checkpoint["ppa_batch_cursor"] >= len(batch["cases"]):
        context = await feedback_context(db, task, run)
        if context["next_phase"] is None:
            task.status = run.status = "COMPLETED"
            run.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
            run.checkpoint = {**run.checkpoint, "ppa_stop_reason": context["stop_reason"]}
            await db.commit()
            return {"done": True, "reason": context["stop_reason"]}
        return await pause(db, task, run, "AWAITING_RESEARCH_BATCH")
    return {"done": False, "cursor": run.checkpoint["ppa_batch_cursor"]}
