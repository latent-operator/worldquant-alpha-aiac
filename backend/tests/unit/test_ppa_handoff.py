import pytest
from copy import deepcopy
from sqlalchemy import select

from backend.models import ExperimentRun, MiningTask, ResearchCandidate
from backend.services import ppa_handoff
from backend.tests.unit.test_ppa_v2_workflow import setup, discovery


async def prepared(maker, task_id, run_id, *, pending=False, incomplete=False):
    cases = discovery()["cases"]
    async with maker() as db:
        task, run = await db.get(MiningTask, task_id), await db.get(ExperimentRun, run_id)
        task.config = {**task.config, "research_protocol": "theme_ppa_v3",
                       "continuous_mode": True, "research_request_budget": None}
        task.status = run.status = "PAUSED"
        run.block_reason = "PPA_FIRST_BATCH_REVIEW"
        run.checkpoint = {"ppa_batch": {"cases": cases}, "ppa_batch_cursor": 3 if incomplete else 4}
        for i, case in enumerate(cases):
            db.add(ResearchCandidate(task_id=task_id, run_id=run_id, source="DISCOVERY", family_id="ppa-test",
                experiment_group=case["case_id"], expression=case["expression"],
                expression_hash=case["case_id"], settings_hash="s", settings={},
                validation_status="VALID", request_status="UNKNOWN" if pending and i == 0 else "RESULT",
                gate_failures=[], metrics={}, result_snapshot={"ppa_evaluation": {"status": "PPA_REJECTED"}}))
        await db.commit()


@pytest.mark.asyncio
async def test_boundary_inspection_is_read_only(async_engine, monkeypatch):
    maker, tid, rid, _ = await setup(async_engine, monkeypatch)
    await prepared(maker, tid, rid)
    async with maker() as db:
        result = await ppa_handoff.inspect_ppa_boundary(db, tid, rid)
        assert result == {"task_id": tid, "run_id": rid, "status": "PAUSED", "cases": 4}
        task, run = await db.get(MiningTask, tid), await db.get(ExperimentRun, rid)
        assert task.status == run.status == "PAUSED"


@pytest.mark.asyncio
async def test_close_only_complete_reconciled_paused_batch(async_engine, monkeypatch):
    maker, tid, rid, _ = await setup(async_engine, monkeypatch)
    await prepared(maker, tid, rid)
    async with maker() as db:
        result = await ppa_handoff.close_ppa_run(db, tid, rid)
        assert result == {"task_id": tid, "run_id": rid, "status": "STOPPED"}
        task, run = await db.get(MiningTask, tid), await db.get(ExperimentRun, rid)
        assert task.status == run.status == "STOPPED"
        assert run.finished_at is not None and run.checkpoint["ppa_batch_cursor"] == 4
        assert len((await db.execute(select(ResearchCandidate).where(ResearchCandidate.run_id == rid))).scalars().all()) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("pending,incomplete", [(True, False), (False, True)])
async def test_close_refuses_pending_or_incomplete_batch(async_engine, monkeypatch, pending, incomplete):
    maker, tid, rid, _ = await setup(async_engine, monkeypatch)
    await prepared(maker, tid, rid, pending=pending, incomplete=incomplete)
    async with maker() as db:
        with pytest.raises(ValueError, match="PPA_HANDOFF_"):
            await ppa_handoff.close_ppa_run(db, tid, rid)
        task, run = await db.get(MiningTask, tid), await db.get(ExperimentRun, rid)
        assert task.status == run.status == "PAUSED"


@pytest.mark.asyncio
async def test_close_refuses_stale_run_and_429_only_case(async_engine, monkeypatch):
    maker, tid, rid, _ = await setup(async_engine, monkeypatch)
    await prepared(maker, tid, rid)
    async with maker() as db:
        with pytest.raises(ValueError, match="PPA_HANDOFF_RUN_STATE_INVALID"):
            await ppa_handoff.close_ppa_run(db, tid, rid - 1)
        row = (await db.execute(select(ResearchCandidate).where(ResearchCandidate.run_id == rid)
                                .order_by(ResearchCandidate.id))).scalars().first()
        row.request_status = "FAILED"
        row.result_snapshot = {"http_status": 429}
        await db.commit()
        with pytest.raises(ValueError, match="PPA_HANDOFF_BATCH_INCOMPLETE"):
            await ppa_handoff.close_ppa_run(db, tid, rid)


@pytest.mark.asyncio
async def test_close_refuses_manual_pause_and_ambiguous_failure(async_engine, monkeypatch):
    maker, tid, rid, _ = await setup(async_engine, monkeypatch)
    await prepared(maker, tid, rid)
    async with maker() as db:
        run = await db.get(ExperimentRun, rid)
        run.block_reason = "USER_PAUSED"
        await db.commit()
        with pytest.raises(ValueError, match="PPA_HANDOFF_RUN_STATE_INVALID"):
            await ppa_handoff.close_ppa_run(db, tid, rid)
        run.block_reason = "PPA_FIRST_BATCH_REVIEW"
        row = (await db.execute(select(ResearchCandidate).where(ResearchCandidate.run_id == rid))).scalars().first()
        row.request_status = "FAILED"
        row.result_snapshot = {}
        await db.commit()
        with pytest.raises(ValueError, match="PPA_HANDOFF_BATCH_INCOMPLETE"):
            await ppa_handoff.close_ppa_run(db, tid, rid)


def expanded_manifest(base):
    result = deepcopy(base)
    result["datasets"]["new_dataset"] = ["new_field"]
    result["dataset_categories"]["new_dataset"] = "analyst"
    result["mechanisms"]["new_signal"] = {"dataset_id": "new_dataset", "hypothesis": "new source",
                                              "direction": "positive"}
    result["discovery_allocation"] = {"signal": 3, "new_signal": 1}
    result["field_metadata"] = {dataset: {name: {"type": "MATRIX", "instrument_coverage": 0.9,
        "date_coverage": 1.0, "account_verified": True, "verified_at": "2026-11-11T00:00:00Z",
        "source_url": "https://platform.worldquantbrain.com/data"} for name in names}
        for dataset, names in result["datasets"].items()}
    return result


@pytest.mark.asyncio
async def test_config_install_is_atomic_and_preserves_closed_run_snapshot(async_engine, monkeypatch):
    maker, tid, rid, base = await setup(async_engine, monkeypatch)
    await prepared(maker, tid, rid)
    async with maker() as db:
        await ppa_handoff.close_ppa_run(db, tid, rid)
        run = await db.get(ExperimentRun, rid)
        old_snapshot = deepcopy(run.config_snapshot)
        proposed = expanded_manifest(base)
        result = await ppa_handoff.install_ppa_config(db, tid, rid, proposed)
        task = await db.get(MiningTask, tid)
        assert result["status"] == "STOPPED"
        assert set(task.target_datasets) == {"dataset1", "new_dataset"}
        assert task.config["ppa_local_expansion"] is False
        assert run.config_snapshot == old_snapshot


@pytest.mark.asyncio
async def test_config_install_rejects_missing_evidence_without_mutation(async_engine, monkeypatch):
    maker, tid, rid, base = await setup(async_engine, monkeypatch)
    await prepared(maker, tid, rid)
    async with maker() as db:
        await ppa_handoff.close_ppa_run(db, tid, rid)
        task = await db.get(MiningTask, tid)
        old = deepcopy(task.config)
        proposed = expanded_manifest(base)
        del proposed["field_metadata"]["new_dataset"]["new_field"]["source_url"]
        with pytest.raises(ValueError, match="PPA_FIELD_EVIDENCE_REQUIRED"):
            await ppa_handoff.install_ppa_config(db, tid, rid, proposed)
        assert task.config == old and task.target_datasets == ["dataset1"]


@pytest.mark.asyncio
async def test_config_install_rechecks_closed_batch_boundary(async_engine, monkeypatch):
    maker, tid, rid, base = await setup(async_engine, monkeypatch)
    await prepared(maker, tid, rid)
    async with maker() as db:
        await ppa_handoff.close_ppa_run(db, tid, rid)
        run = await db.get(ExperimentRun, rid)
        run.checkpoint = {**run.checkpoint, "ppa_batch_cursor": 3}
        await db.commit()
        with pytest.raises(ValueError, match="PPA_HANDOFF_BATCH_INCOMPLETE"):
            await ppa_handoff.install_ppa_config(db, tid, rid, expanded_manifest(base))
