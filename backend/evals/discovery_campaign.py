"""Frozen, serial PPA discovery batches in an independent unbounded lab run."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from backend.alpha_semantic_validator import AlphaSemanticValidator
from backend.evals.live_runner import _fields, _rows, _set_state
from backend.models import ExperimentRun, MiningTask, ResearchCandidate
from backend.services.ppa_feedback import case_settings, digest, freeze_batch
from backend.services.research_audit import ResearchBudgetBrain, ResearchLedger
from backend.services.theme_ppa_v2 import persist_result


PREFERRED_DATASETS = ("sentiment26", "news76", "analyst10")
CATEGORY_ORDER = ("sentiment", "news", "analyst", "fundamental", "earnings")
PENDING = frozenset({"RESERVED", "REQUESTED", "UNKNOWN"})


def choose_dataset(datasets: list[dict], fields_by_id: dict[str, list[dict]],
                   *, excluded: set[str]) -> dict:
    """Use only live, account-visible matrix fields; do not infer vector aggregators."""
    by_id = {str(item.get("id")): item for item in datasets if isinstance(item, dict)}
    fallback = sorted((item for item in by_id.values()
                       if item.get("category") in CATEGORY_ORDER),
                      key=lambda item: (CATEGORY_ORDER.index(item["category"]),
                                        -float(item.get("coverage") or 0), str(item["id"])))
    order = [by_id[name] for name in PREFERRED_DATASETS if name in by_id]
    order.extend(item for item in fallback if item["id"] not in PREFERRED_DATASETS)
    for item in order:
        dataset = str(item["id"])
        if dataset in excluded:
            continue
        fields = [field for field in fields_by_id.get(dataset, [])
                  if isinstance(field, dict) and field.get("type") == "MATRIX"
                  and isinstance(field.get("id"), str)
                  and isinstance(field.get("coverage"), (int, float))
                  and isinstance(field.get("dateCoverage"), (int, float))
                  and 0 < field["coverage"] <= 1 and 0 < field["dateCoverage"] <= 1]
        if len({field["id"] for field in fields}) >= 2:
            return {"dataset_id": dataset, "category": item.get("category"),
                    "field_ids": sorted({field["id"] for field in fields}),
                    "field_metadata": {field["id"]: {
                        "type": "MATRIX", "account_verified": True,
                        "instrument_coverage": field["coverage"],
                        "date_coverage": field["dateCoverage"]} for field in fields}}
    raise ValueError("NO_ACCOUNT_VERIFIED_NEW_DATASET")


def freeze_campaign_batch(campaign: dict, evidence: dict, proposal: dict, *, index: int) -> dict:
    if index < 1 or evidence.get("budget_remaining") is not None:
        raise ValueError("CAMPAIGN_BATCH_INVALID")
    frozen = freeze_batch(campaign["base"], evidence, proposal)
    if not 1 <= len(frozen["cases"]) <= 16 or len(frozen["cases"]) % 4:
        raise ValueError("CAMPAIGN_BATCH_SIZE_INVALID")
    payload = {"schema_version": 1, "campaign_id": campaign["campaign_id"],
               "index": index, "code_commit": campaign["code_commit"],
               "evidence": evidence, "cases": frozen["cases"],
               "generation": proposal.get("candidate_generation", frozen["candidate_generation"]),
               "send_order": sorted((case["case_id"] for case in frozen["cases"]),
                                    key=lambda cid: digest(["campaign-send", index, cid]))}
    result = {**payload, "batch_sha256": digest(payload)}
    validate_campaign_batch(campaign, result)
    return result


def validate_campaign_batch(campaign: dict, batch: dict) -> None:
    if digest({key: value for key, value in batch.items() if key != "batch_sha256"}) != batch.get("batch_sha256"):
        raise ValueError("BATCH_HASH_MISMATCH")
    if batch.get("campaign_id") != campaign.get("campaign_id") or batch.get("code_commit") != campaign.get("code_commit"):
        raise ValueError("CAMPAIGN_ID_MISMATCH")
    cases = batch.get("cases") or []
    if not 1 <= len(cases) <= 16 or len(cases) % 4 or len(set(batch.get("send_order") or [])) != len(cases):
        raise ValueError("CAMPAIGN_BATCH_SIZE_INVALID")
    checked = freeze_batch(campaign["base"], batch["evidence"],
                           {"phase": "discovery", "cases": cases})
    if checked["cases"] != cases or set(batch["send_order"]) != {case["case_id"] for case in cases}:
        raise ValueError("CAMPAIGN_BATCH_CONSTRAINT_MISMATCH")
    fields = campaign["base"]["datasets"][campaign["dataset_id"]]
    validator = AlphaSemanticValidator(fields=[{"id": field, "type": "MATRIX"} for field in fields],
                                       strict_field_check=True)
    if any(case["dataset_id"] != campaign["dataset_id"] or
           not set(validator.validate(case["expression"]).used_fields) & set(fields)
           for case in cases):
        raise ValueError("CAMPAIGN_DATASET_FIELD_REQUIRED")


def campaign_gate(state: dict, base: dict) -> str:
    if state.get("source_unresolved") != 0:
        return "SOURCE_REQUEST_PENDING"
    if state.get("source_run_current") is False:
        return "SOURCE_RUN_CHANGED"
    if state.get("other_active_work"):
        return "OTHER_WQ_WORK_ACTIVE"
    if not state.get("platform_verified"):
        return "PLATFORM_UNVERIFIED"
    try:
        ends = datetime.fromisoformat(base["ends_at"].replace("Z", "+00:00"))
        now = state.get("now") or datetime.now(timezone.utc)
        if now.tzinfo is None or now >= ends:
            return "THEME_EXPIRED"
    except (TypeError, ValueError, KeyError):
        return "THEME_EXPIRED"
    return "READY"


async def _open_campaign_run(maker, campaign: dict) -> tuple[int, int]:
    async with maker() as db:
        runs = (await db.execute(select(ExperimentRun))).scalars().all()
        matching = [run for run in runs if (run.checkpoint or {}).get("campaign_id") == campaign["campaign_id"]]
        if len(matching) > 1:
            raise ValueError("DUPLICATE_CAMPAIGN_RUN")
        if matching:
            return matching[0].task_id, matching[0].id
        base = campaign["base"]
        config = {"research_protocol": "eval_ppa_discovery_v1", "ppa_manifest": base,
                  "research_request_budget": None, "audit_candidates": True,
                  "sharpe_min": 1.58, "fitness_min": 1.0,
                  "turnover_min": 0.05, "turnover_max": 0.30}
        task = MiningTask(task_name="PPA discovery " + campaign["campaign_id"][:12],
                          region=base["region"], universe=base["universe"],
                          dataset_strategy="SPECIFIC", target_datasets=list(base["datasets"]),
                          agent_mode="MANUAL", status="RUNNING", daily_goal=0,
                          max_iterations=1, config=config)
        db.add(task)
        await db.flush()
        run = ExperimentRun(task_id=task.id, status="RUNNING", trigger_source="EVAL_LAB",
                            config_snapshot={"task": {"config": config}},
                            checkpoint={"campaign_id": campaign["campaign_id"],
                                        "source_task_id": campaign["source_task_id"],
                                        "source_run_id": campaign["source_run_id"],
                                        "completed_batches": []})
        db.add(run)
        await db.commit()
        return task.id, run.id


async def run_campaign_batch(campaign: dict, batch: dict, *, maker, brain, live_state,
                             quality_evaluator=None, max_steps: int | None = None) -> dict:
    validate_campaign_batch(campaign, batch)
    reason = campaign_gate(dict(live_state()), campaign["base"])
    if reason != "READY":
        return {"status": "BLOCKED", "reason": reason, "sent": 0}
    task_id, run_id = await _open_campaign_run(maker, campaign)
    ledger = ResearchLedger(task_id, run_id, None,
                            family_id="ppa-discovery-" + campaign["campaign_id"][:16],
                            dedup_task_ids=[campaign["old_lab_task_id"]])
    wrapped = ResearchBudgetBrain(brain, ledger)
    cases = {case["case_id"]: case for case in batch["cases"]}
    sent = 0
    async with maker() as db:
        task, run = await db.get(MiningTask, task_id), await db.get(ExperimentRun, run_id)
        if run.block_reason in {"QUALITY_PASS_REVIEW", "EVAL_RATE_LIMIT_REVIEW"}:
            return {"status": "PAUSED", "reason": run.block_reason, "sent": 0,
                    "task_id": task_id, "run_id": run_id}
        if batch["batch_sha256"] in (run.checkpoint or {}).get("completed_batches", []):
            return {"status": "COMPLETE", "reason": "EVAL_BATCH_COMPLETE", "sent": 0,
                    "task_id": task_id, "run_id": run_id}
        active = (run.checkpoint or {}).get("active_batch")
        if active and active != batch["batch_sha256"]:
            raise ValueError("OTHER_CAMPAIGN_BATCH_ACTIVE")
        if run.block_reason == "EVAL_RATE_LIMIT" and run.next_retry_at:
            until = run.next_retry_at.replace(tzinfo=timezone.utc) if run.next_retry_at.tzinfo is None else run.next_retry_at
            if datetime.now(timezone.utc) < until:
                return {"status": "PAUSED", "reason": "EVAL_RATE_LIMIT", "sent": 0,
                        "task_id": task_id, "run_id": run_id}
        if quality_evaluator is None:
            from backend.agents.mining_agent import MiningAgent
            quality_evaluator = MiningAgent(db, wrapped)._quality_status_for_optimization_result
        async def qualify_unfinished():
            for row in await _rows(db, run_id):
                if row.request_status != "RESULT" or (row.result_snapshot or {}).get("ppa_evaluation"):
                    continue
                case = cases.get(row.experiment_group)
                if case is None:
                    raise ValueError("CAMPAIGN_CASE_NOT_IN_BATCH")
                result = {**(row.result_snapshot or {}), "alpha_id": row.wq_alpha_id}
                await persist_result(db, wrapped, task, run, case,
                                     case_settings(campaign["base"], {}), result,
                                     "discovery", quality_evaluator,
                                     _fields({"base": campaign["base"]}, case))
        pending = [row for row in await _rows(db, run_id) if row.request_status in PENDING]
        if pending:
            async def poll(location):
                result = await brain.poll_existing_simulation(location)
                if result.get("alpha_id"):
                    return result
                if str(result.get("status") or "").upper() in {"ERROR", "FAILED"}:
                    return {**result, "request_phase": "POLL_ORIGINAL",
                            "error": "original simulation failed"}
                return {"error": "uncertain original pending"}
            await ledger.reconcile(poll)
            await qualify_unfinished()
            if any(row.request_status in PENDING for row in await _rows(db, run_id)):
                await _set_state(db, task, run, "PAUSED", "EVAL_REQUEST_RECONCILIATION_PENDING")
                return {"status": "PAUSED", "reason": run.block_reason, "sent": 0,
                        "task_id": task_id, "run_id": run_id}
        await qualify_unfinished()
        if any(row.quality_status == "PASS" for row in await _rows(db, run_id)):
            await _set_state(db, task, run, "PAUSED", "QUALITY_PASS_REVIEW")
            return {"status": "PAUSED", "reason": run.block_reason, "sent": 0,
                    "task_id": task_id, "run_id": run_id}
        run.checkpoint = {**(run.checkpoint or {}), "active_batch": batch["batch_sha256"]}
        await _set_state(db, task, run, "RUNNING", None)
        for case_id in batch["send_order"]:
            rows = await _rows(db, run_id)
            prior = next((row for row in reversed(rows) if row.experiment_group == case_id), None)
            if prior and prior.request_status == "RESULT":
                continue
            if prior and prior.request_status == "FAILED":
                continue
            if max_steps is not None and sent >= max_steps:
                await _set_state(db, task, run, "PAUSED", "EVAL_BATCH_LIMIT_REACHED")
                break
            try:
                reason = campaign_gate(dict(live_state()), campaign["base"])
            except Exception:
                reason = "EVAL_SOURCE_CHECK_FAILED"
            if reason != "READY":
                await _set_state(db, task, run, "PAUSED", reason)
                break
            case = cases[case_id]
            settings = case_settings(campaign["base"], {})
            wrapped.register(case["expression"], group=case_id, arm=case["arm"],
                             pair_id=case["pair_id"], research_case=case)
            with wrapped.source("EVAL_BACKTEST"):
                result = await wrapped.simulate_alpha(case["expression"], **settings)
            sent += 1
            if result.get("alpha_id"):
                try:
                    await persist_result(db, wrapped, task, run, case, settings, result,
                                         "discovery", quality_evaluator, _fields(
                                             {"base": campaign["base"]}, case))
                except Exception:
                    await _set_state(db, task, run, "PAUSED", "EVAL_QUALIFICATION_ERROR")
                    raise
            row = next((item for item in reversed(await _rows(db, run_id))
                        if item.experiment_group == case_id), None)
            if row is None or row.request_status == "NOT_REQUESTED":
                await _set_state(db, task, run, "PAUSED", "EVAL_CANDIDATE_NOT_SENT")
                break
            if row.request_status in PENDING:
                await _set_state(db, task, run, "PAUSED", "EVAL_REQUEST_RECONCILIATION_PENDING")
                break
            if result.get("http_status") == 429:
                streak = int((run.checkpoint or {}).get("rate_limit_streak", 0)) + 1
                run.checkpoint = {**run.checkpoint, "rate_limit_streak": streak}
                wait = result.get("retry_after")
                run.next_retry_at = datetime.now(timezone.utc) + timedelta(
                    seconds=max(1, wait if isinstance(wait, (int, float)) and 0 <= wait <= 86400 else 60))
                await _set_state(db, task, run, "PAUSED",
                                 "EVAL_RATE_LIMIT_REVIEW" if streak >= 3 else "EVAL_RATE_LIMIT")
                break
            run.checkpoint = {**run.checkpoint, "rate_limit_streak": 0}
            if row.quality_status == "PASS":
                await _set_state(db, task, run, "PAUSED", "QUALITY_PASS_REVIEW")
                break
        else:
            run.checkpoint = {**run.checkpoint, "active_batch": None,
                              "completed_batches": [*(run.checkpoint or {}).get("completed_batches", []),
                                                    batch["batch_sha256"]]}
            await _set_state(db, task, run, "PAUSED", "EVAL_BATCH_COMPLETE")
        return {"status": run.status, "reason": run.block_reason, "sent": sent,
                "task_id": task_id, "run_id": run_id}
