"""Single-process, finite WQ experiment using AIAC's audited request ledger."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from backend.models import ExperimentRun, MiningTask, ResearchCandidate
from backend.services.ppa_feedback import digest
from backend.services.research_audit import ResearchBudgetBrain, ResearchLedger
from backend.services.theme_ppa_v2 import persist_result
from backend.evals.live_ppa import dispatch_gate


async def _set_state(db, task, run, status: str, reason: str | None) -> None:
    task.status = run.status = status
    run.block_reason = reason
    if status == "STOPPED":
        run.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()


async def _open_lab_run(maker, manifest: dict):
    async with maker() as db:
        runs = (await db.execute(select(ExperimentRun))).scalars().all()
        matching = [run for run in runs
                    if (run.checkpoint or {}).get("manifest_sha256") == manifest["manifest_sha256"]]
        if len(matching) > 1:
            raise ValueError("DUPLICATE_LAB_RUN")
        if matching:
            run = matching[0]
            task = await db.get(MiningTask, run.task_id)
            if task is None:
                raise ValueError("LAB_TASK_MISSING")
            return task.id, run.id
        config = {"research_protocol": "eval_ppa_shadow_v1",
                  "ppa_manifest": manifest["base"], "audit_candidates": True,
                  "research_request_budget": manifest["budget"],
                  "sharpe_min": 1.58, "fitness_min": 1.0,
                  "turnover_min": 0.05, "turnover_max": 0.30}
        task = MiningTask(task_name="PPA eval lab " + manifest["manifest_sha256"][:12],
                          region=manifest["base"]["region"], universe=manifest["base"]["universe"],
                          dataset_strategy="SPECIFIC",
                          target_datasets=list(manifest["base"]["datasets"]),
                          agent_mode="MANUAL", status="RUNNING", daily_goal=0,
                          max_iterations=1, config=config)
        db.add(task)
        await db.flush()
        run = ExperimentRun(task_id=task.id, status="RUNNING", trigger_source="EVAL_LAB",
                            config_snapshot={"task": {"config": config}},
                            checkpoint={"manifest_sha256": manifest["manifest_sha256"],
                                        "source_task_id": manifest["source_task_id"],
                                        "source_run_id": manifest["source_run_id"]})
        db.add(run)
        await db.commit()
        return task.id, run.id


async def _rows(db, run_id: int) -> list[ResearchCandidate]:
    return (await db.execute(select(ResearchCandidate).where(ResearchCandidate.run_id == run_id)
                             .order_by(ResearchCandidate.id)
                             .execution_options(populate_existing=True))).scalars().all()


async def run_lab(manifest: dict, *, maker, brain, live_state,
                  quality_evaluator=None, max_steps: int | None = None,
                  lab_database: str = "aiac_eval_ppa", lab_port: int = 55434) -> dict:
    """Never send when the production or lab ledger has an unresolved request."""
    if digest({k: v for k, v in manifest.items() if k != "manifest_sha256"}) != manifest.get("manifest_sha256"):
        raise ValueError("MANIFEST_HASH_MISMATCH")
    if len(manifest["candidates"]) != manifest["budget"] or len(set(manifest["send_order"])) != manifest["budget"]:
        raise ValueError("MANIFEST_SIZE_INVALID")
    state = dict(live_state())
    state.update(lab_database=lab_database, lab_port=lab_port, budget=manifest["budget"],
                 theme_ends_at=manifest["base"]["ends_at"], existing_requests=0,
                 lab_unresolved=0, now=state.get("now") or datetime.now(timezone.utc))
    reason = dispatch_gate(state)
    if reason != "READY":
        return {"status": "BLOCKED", "reason": reason, "sent": 0}

    task_id, run_id = await _open_lab_run(maker, manifest)
    ledger = ResearchLedger(task_id, run_id, manifest["budget"],
                            family_id="ppa-eval-" + manifest["manifest_sha256"][:16])
    wrapped = ResearchBudgetBrain(brain, ledger)
    cases = {case["case_id"]: case for case in manifest["candidates"]}
    sent = 0

    async with maker() as db:
        task, run = await db.get(MiningTask, task_id), await db.get(ExperimentRun, run_id)
        if run.status == "STOPPED":
            return {"status": "COMPLETE", "reason": run.block_reason, "sent": 0,
                    "task_id": task_id, "run_id": run_id}
        if run.block_reason == "EVAL_RATE_LIMIT" and run.next_retry_at is not None:
            retry_at = run.next_retry_at
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) < retry_at:
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
                    raise ValueError("LAB_CASE_NOT_IN_MANIFEST")
                result = dict(row.result_snapshot or {})
                result["alpha_id"] = row.wq_alpha_id
                await persist_result(db, wrapped, task, run, case, case["settings"], result,
                                     "refinement", quality_evaluator, _fields(manifest, case))

        pending = [row for row in await _rows(db, run_id)
                   if row.request_status in {"RESERVED", "REQUESTED", "UNKNOWN"}]
        if pending:
            async def poll_original(location):
                result = await brain.poll_existing_simulation(location)
                # A transport error or absent Alpha is not proof that the POST failed.
                return result if result.get("alpha_id") else {
                    "error": "uncertain original simulation still pending"}

            await ledger.reconcile(poll_original)
            await qualify_unfinished()
            if any(row.request_status in {"RESERVED", "REQUESTED", "UNKNOWN"}
                   for row in await _rows(db, run_id)):
                await _set_state(db, task, run, "PAUSED", "EVAL_REQUEST_RECONCILIATION_PENDING")
                return {"status": "BLOCKED", "reason": run.block_reason, "sent": 0,
                        "task_id": task_id, "run_id": run_id}
        await qualify_unfinished()
        run.next_retry_at = None
        await _set_state(db, task, run, "RUNNING", None)

        for case_id in manifest["send_order"]:
            rows = await _rows(db, run_id)
            existing = {row.experiment_group: row for row in rows}
            if case_id in existing:
                continue
            if max_steps is not None and sent >= max_steps:
                await _set_state(db, task, run, "PAUSED", "EVAL_BATCH_LIMIT_REACHED")
                break
            try:
                state = dict(live_state())
            except Exception:
                await _set_state(db, task, run, "PAUSED", "EVAL_SOURCE_CHECK_FAILED")
                break
            state.update(lab_database=lab_database, lab_port=lab_port, budget=manifest["budget"],
                         theme_ends_at=manifest["base"]["ends_at"],
                         existing_requests=sum(row.request_status in {
                             "RESERVED", "REQUESTED", "UNKNOWN", "FAILED", "RESULT"} for row in rows),
                         lab_unresolved=sum(row.request_status in {"RESERVED", "REQUESTED", "UNKNOWN"}
                                            for row in rows),
                         now=state.get("now") or datetime.now(timezone.utc))
            reason = dispatch_gate(state)
            if reason != "READY":
                await _set_state(db, task, run, "PAUSED", reason)
                break
            case = cases[case_id]
            wrapped.register(case["expression"], group=case_id, arm="TREATMENT",
                             research_case=case)
            with wrapped.source("EVAL_BACKTEST"):
                result = await wrapped.simulate_alpha(case["expression"], **case["settings"])
            sent += 1
            if result.get("alpha_id"):
                try:
                    await persist_result(db, wrapped, task, run, case, case["settings"], result,
                                         "refinement", quality_evaluator, _fields(manifest, case))
                except Exception:
                    await _set_state(db, task, run, "PAUSED", "EVAL_QUALIFICATION_ERROR")
                    raise
            row = (await db.execute(select(ResearchCandidate).where(
                ResearchCandidate.run_id == run_id, ResearchCandidate.experiment_group == case_id)
                .order_by(ResearchCandidate.id.desc()).limit(1)
                .execution_options(populate_existing=True))).scalar_one_or_none()
            if row is None:
                await _set_state(db, task, run, "PAUSED", "EVAL_LEDGER_ROW_MISSING")
                break
            if row.request_status in {"RESERVED", "REQUESTED", "UNKNOWN"}:
                await _set_state(db, task, run, "PAUSED", "EVAL_REQUEST_RECONCILIATION_PENDING")
                break
            if result.get("http_status") == 429:
                wait = result.get("retry_after")
                if not isinstance(wait, (int, float)) or not 0 <= wait <= 86400:
                    wait = 60
                run.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=max(1, wait))
                await _set_state(db, task, run, "PAUSED", "EVAL_RATE_LIMIT")
                break
        else:
            rows = await _rows(db, run_id)
            if len({row.experiment_group for row in rows}) == manifest["budget"]:
                await _set_state(db, task, run, "STOPPED", "EVAL_COMPLETED")
        return {"status": run.status, "reason": run.block_reason, "sent": sent,
                "task_id": task_id, "run_id": run_id}


def _fields(manifest: dict, case: dict) -> list[dict]:
    dataset = case["dataset_id"]
    metadata = (manifest["base"].get("field_metadata") or {}).get(dataset) or {}
    return [{"id": name, "type": (metadata.get(name) or {}).get("type", "MATRIX")}
            for name in manifest["base"]["datasets"][dataset]]
