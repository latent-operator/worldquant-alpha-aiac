from copy import deepcopy
from types import SimpleNamespace
import asyncio
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from backend.models import MiningTask, ExperimentRun, ResearchCandidate, Alpha
from backend.services import research_audit as audit
from backend.services import theme_ppa_v2 as workflow
from backend.services.ppa_feedback import freeze_batch
from backend.services.research_report import research_report
from backend.tests.unit.test_theme_ppa import manifest


async def setup(async_engine, monkeypatch):
    maker = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(audit, "AsyncSessionLocal", maker)
    m = manifest()
    m.update(starts_at="2026-01-01T00:00:00Z", ends_at="2027-01-01T00:00:00Z",
             confirmation_settings={"decay": [2], "neutralization": ["INDUSTRY"]},
             mechanisms={"signal": {"dataset_id": "dataset1", "hypothesis": "signal", "direction": "positive"}},
             historical_mechanisms={"p0": "signal"},
             theme_requirements={"amer_sharpe_strictly_greater_than": 1})
    async with maker() as db:
        old = MiningTask(task_name="old", region="USA", universe="TOP3000", status="PAUSED")
        db.add(old)
        await db.flush()
        root = ExperimentRun(task_id=old.id, status="PAUSED", config_snapshot={"task": {"config": {
            "research_protocol": "theme_ppa_v1", "ppa_manifest": m}}})
        db.add(root)
        await db.flush()
        cfg = {"research_protocol": "theme_ppa_v2", "research_request_budget": 80,
               "budget_root_run_id": root.id, "ppa_manifest": m}
        task = MiningTask(task_name="new", region="USA", universe="TOP3000", status="PAUSED",
                          dataset_strategy="SPECIFIC", target_datasets=["dataset1"], config=cfg)
        db.add(task)
        await db.flush()
        run = ExperimentRun(task_id=task.id, status="PAUSED", block_reason="AWAITING_RESEARCH_BATCH",
                            config_snapshot={"task": {"config": cfg}})
        db.add(run)
        await db.commit()
        return maker, task.id, run.id, m


def discovery():
    return {"phase": "discovery", "cases": [{"case_id": f"new{i}", "pair_id": "newpair",
        "arm": "CONTROL" if i == 0 else "TREATMENT", "dataset_id": "dataset1",
        "expression": f"rank(field_0_{i})", "mechanism_id": "signal",
        "changed_element": "field", "expected_effect": "AMER improvement",
        "risk": "noise", "stop_condition": "no gain"} for i in range(4)]}


class Brain:
    calls = 0
    async def simulate_single(self, expression, **settings):
        self.calls += 1
        return {"success": True, "alpha_id": f"alpha{self.calls}", "stage": "IS", "status": "UNSUBMITTED",
                "platform_can_submit": True, "classifications": [{"id": "POWER_POOL_ELIGIBLE"}],
                "themes": [{"id": "theme-2026-11"}], "checks": [{"name": "MATCHES_THEMES", "result": "PASS"}],
                "metrics": {"amer_sharpe": 1.2, "sharpe": 1.4, "fitness": 0.8, "test_sharpe": 0.6,
                            "two_year_sharpe": 0.7, "turnover": 0.2, "margin": 0.001,
                            "long_count": 100, "short_count": 100}}
    async def check_power_pool_correlation(self, alpha):
        return {"max": 0.2}
    async def poll_existing_simulation(self, location):
        pytest.fail("No unresolved requests")


@pytest.mark.asyncio
@pytest.mark.parametrize("transport,expected_pairs", [("single", 0), ("paired_multi", 2)])
async def test_continuous_batch_overlaps_independent_wq_requests(async_engine, monkeypatch,
                                                                  transport, expected_pairs):
    maker, tid, rid, base = await setup(async_engine, monkeypatch)
    class ConcurrentBrain(Brain):
        active = 0
        peak = 0
        pairs = 0
        async def simulate_single(self, expression, **settings):
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.03)
            self.active -= 1
            return await super().simulate_single(expression, **settings)
        async def simulate_audited_pair(self, expressions, **settings):
            self.pairs += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.03)
            self.active -= 1
            return [await super(ConcurrentBrain, self).simulate_single(expression, **settings)
                    for expression in expressions]
    async def quality(**kwargs):
        return "OPTIMIZE"
    class PreparedLedger:
        def __init__(self, *args, **kwargs):
            pass
        async def recover(self, *args):
            return None
        async def reserve(self, expression, settings, source, metadata=None):
            return 1, None
        async def mark_requested(self, candidate_id):
            pass
        async def finish(self, candidate_id, result):
            pass
    # The in-memory SQLite fixture has one shared connection, so isolate the
    # network overlap test from ledger writes; ledger locking has separate tests.
    monkeypatch.setattr(workflow, "ResearchLedger", PreparedLedger)
    brain = ConcurrentBrain()
    async with maker() as db:
        task, run = await db.get(MiningTask, tid), await db.get(ExperimentRun, rid)
        task.config = {**task.config, "research_protocol": "theme_ppa_v3",
                       "research_request_budget": None, "continuous_mode": True}
        run.checkpoint = {"ppa_phase_limit": 16, "ppa_transport": transport,
                          "ppa_max_in_flight": 2}
        run.config_snapshot = {"task": {"config": task.config}}
        await db.commit()
        evidence = await workflow.feedback_context(db, task, run)
        await workflow.import_batch(db, task, run, freeze_batch(base, evidence, discovery()))
        for case in discovery()["cases"]:
            db.add(ResearchCandidate(task_id=tid, run_id=rid, source="DISCOVERY",
                family_id="theme_ppa_v3:nov", experiment_group=case["case_id"],
                expression=case["expression"], expression_hash=case["case_id"],
                settings_hash="settings", settings={}, validation_status="VALID",
                request_status="NOT_REQUESTED", gate_failures=[], metrics={},
                result_snapshot={"research_case": case}))
        task.status = run.status = "RUNNING"
        await db.commit()
        fields = {"dataset1": [{"id": name, "type": "MATRIX"} for name in base["datasets"]["dataset1"]]}
        await workflow.run_theme_v2(db, brain, task, run, fields, quality_evaluator=quality)
        assert brain.peak >= 2
        assert brain.pairs == expected_pairs
        assert len((await db.execute(select(ResearchCandidate).where(
            ResearchCandidate.run_id == rid))).scalars().all()) == 4


@pytest.mark.asyncio
async def test_parallel_429_stops_unsent_cases(async_engine, monkeypatch):
    maker, tid, rid, base = await setup(async_engine, monkeypatch)
    class PreparedLedger:
        def __init__(self, *args, **kwargs): pass
        async def recover(self, *args): return None
        async def reserve(self, *args, **kwargs): return 1, None
        async def mark_requested(self, *args): pass
        async def finish(self, *args): pass
    monkeypatch.setattr(workflow, "ResearchLedger", PreparedLedger)
    class LimitedBrain(Brain):
        calls = 0
        async def simulate_single(self, expression, **settings):
            self.calls += 1
            return {"success": False, "http_status": 429, "retry_after": 30,
                    "error": "rate limited"}
    async def quality(**kwargs): return "OPTIMIZE"
    brain = LimitedBrain()
    async with maker() as db:
        task, run = await db.get(MiningTask, tid), await db.get(ExperimentRun, rid)
        task.config = {**task.config, "research_protocol": "theme_ppa_v3",
                       "research_request_budget": None, "continuous_mode": True}
        run.checkpoint = {"ppa_phase_limit": 16, "ppa_max_in_flight": 2}
        run.config_snapshot = {"task": {"config": task.config}}
        await db.commit()
        evidence = await workflow.feedback_context(db, task, run)
        await workflow.import_batch(db, task, run, freeze_batch(base, evidence, discovery()))
        for case in discovery()["cases"]:
            db.add(ResearchCandidate(task_id=tid, run_id=rid, source="DISCOVERY",
                family_id="theme_ppa_v3:nov", experiment_group=case["case_id"],
                expression=case["expression"], expression_hash=case["case_id"],
                settings_hash="settings", settings={}, validation_status="VALID",
                request_status="NOT_REQUESTED", gate_failures=[], metrics={},
                result_snapshot={"research_case": case}))
        task.status = run.status = "RUNNING"
        await db.commit()
        fields = {"dataset1": [{"id": name, "type": "MATRIX"} for name in base["datasets"]["dataset1"]]}
        result = await workflow.run_theme_v2(db, brain, task, run, fields, quality_evaluator=quality)
        assert result["reason"] == "PPA_RATE_LIMIT"
        assert brain.calls <= 2
        assert run.next_retry_at is not None


@pytest.mark.asyncio
async def test_legacy_auto_epoch_boundary_upgrades_capacity_before_import(async_engine, monkeypatch):
    maker, tid, rid, base = await setup(async_engine, monkeypatch)
    async with maker() as db:
        task, run = await db.get(MiningTask, tid), await db.get(ExperimentRun, rid)
        task.config = {**task.config, "research_protocol": "theme_ppa_v3",
                       "research_request_budget": None, "continuous_mode": True}
        run.checkpoint = {"ppa_epoch": 4, "ppa_batch": {"cases": [{"case_id": "old1"},
            {"case_id": "old2"}]}, "ppa_batch_cursor": 2}
        await db.commit()
        evidence = await workflow.feedback_context(db, task, run)
        assert evidence["phase_limit"] == 16
        assert evidence["max_batch_cases"] == 16
        await workflow.import_batch(db, task, run, freeze_batch(base, evidence, discovery()))
        assert run.checkpoint["ppa_phase_limit"] == 16


@pytest.mark.asyncio
async def test_existing_alpha_id_in_historical_run_does_not_fail_new_run(async_engine, monkeypatch):
    maker, tid, rid, base = await setup(async_engine, monkeypatch)
    async def quality(**kwargs): return "OPTIMIZE"
    async with maker() as db:
        task, run = await db.get(MiningTask, tid), await db.get(ExperimentRun, rid)
        old_run = await db.get(ExperimentRun, task.config["budget_root_run_id"])
        db.add(Alpha(alpha_id="alpha1", expression="rank(old_field)", region="USA",
                     universe="TOP3000", task_id=old_run.task_id, run_id=old_run.id, status="simulated"))
        await db.commit()
        context = await workflow.feedback_context(db, task, run)
        await workflow.import_batch(db, task, run, freeze_batch(base, context, discovery()))
        task.status = run.status = "RUNNING"
        await db.commit()
        fields = {"dataset1": [{"id": name, "type": "MATRIX"} for name in base["datasets"]["dataset1"]]}
        await workflow.run_theme_v2(db, Brain(), task, run, fields, quality_evaluator=quality)
        assert len((await db.execute(select(ResearchCandidate).where(ResearchCandidate.run_id == rid))).scalars().all()) == 4
        assert len((await db.execute(select(Alpha).where(Alpha.alpha_id == "alpha1"))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_discovery_winner_can_confirm_and_finish_without_forced_refinement(async_engine, monkeypatch):
    maker, tid, rid, base = await setup(async_engine, monkeypatch)
    async def quality(**kwargs): return "OPTIMIZE"
    brain = Brain()
    async with maker() as db:
        task, run = await db.get(MiningTask, tid), await db.get(ExperimentRun, rid)
        context = await workflow.feedback_context(db, task, run)
        batch = freeze_batch(base, context, discovery())
        await workflow.import_batch(db, task, run, batch)
        task.status = run.status = "RUNNING"
        await db.commit()
        fields = {"dataset1": [{"id": name, "type": "MATRIX"} for name in base["datasets"]["dataset1"]]}
        await workflow.run_theme_v2(db, brain, task, run, fields, quality_evaluator=quality)
        assert brain.calls == 4
        assert task.status == "PAUSED"
        assert run.block_reason == "PPA_FIRST_BATCH_REVIEW"
        report = await research_report(db, rid)
        assert report["budget"] == {"limit": 80, "reserved": 4, "remaining": 76}
        assert report["ppa_v2_progress"]["best_amer_positive_test"]["amer_sharpe"] == 1.2
        assert len((await db.execute(select(Alpha))).scalars().all()) == 4
        context = await workflow.feedback_context(db, task, run)
        assert context["next_phase"] == "confirmation"
        batch = freeze_batch(base, context, {"phase": "confirmation", "cases": []})
        await workflow.import_batch(db, task, run, batch)
        task.status = run.status = "RUNNING"
        await db.commit()
        await workflow.run_theme_v2(db, brain, task, run, fields, quality_evaluator=quality)
        assert task.status == "COMPLETED"
        rows = (await db.execute(select(ResearchCandidate))).scalars().all()
        assert sum((r.result_snapshot or {}).get("ppa_evaluation", {}).get("status") == "PPA_READY" for r in rows) == 1
        assert brain.calls == 6


@pytest.mark.asyncio
async def test_missing_batch_and_paused_tasks_never_simulate(async_engine, monkeypatch):
    maker, tid, rid, base = await setup(async_engine, monkeypatch)
    async with maker() as db:
        task, run = await db.get(MiningTask, tid), await db.get(ExperimentRun, rid)
        brain = Brain()
        await workflow.run_theme_v2(db, brain, task, run, {})
        assert brain.calls == 0
        task.status = run.status = "RUNNING"
        await db.commit()
        await workflow.run_theme_v2(db, brain, task, run, {})
        assert brain.calls == 0 and task.status == "PAUSED"
        assert run.block_reason == "AWAITING_RESEARCH_BATCH"


@pytest.mark.asyncio
async def test_paused_unknown_is_polled_from_original_location_and_fully_evaluated(async_engine, monkeypatch):
    maker, tid, rid, base = await setup(async_engine, monkeypatch)
    class PollOnly(Brain):
        polls = 0
        async def simulate_single(self, *args, **kwargs):
            pytest.fail("Recovery must never create another simulation")
        async def poll_existing_simulation(self, location):
            assert location == "/simulations/original"
            self.polls += 1
            return {"success": True, "alpha_id": "recovered-alpha", "stage": "IS",
                    "status": "UNSUBMITTED", "checks": [{"name": "MATCHES_THEMES", "result": "PASS"}],
                    "metrics": {"sharpe": 0.8, "fitness": 0.4, "test_sharpe": 0.3,
                                "amer_sharpe": 0.7, "turnover": 0.2, "margin": 0.001,
                                "long_count": 100, "short_count": 100}}
    async def quality(**kwargs): return "OPTIMIZE"
    case = discovery()["cases"][0]
    case["epoch"] = 1
    settings = {**base["settings"], "region": "USA", "universe": "TOP3000", "delay": 1}
    async with maker() as db:
        task, run = await db.get(MiningTask, tid), await db.get(ExperimentRun, rid)
        task.config = {**task.config, "research_protocol": "theme_ppa_v3", "continuous_mode": True,
                       "research_request_budget": None}
        run.block_reason = "PPA_REQUEST_RECONCILIATION_PENDING"
        run.checkpoint = {"ppa_epoch": 1, "ppa_batch": {"cases": discovery()["cases"]}, "ppa_batch_cursor": 1}
        db.add(ResearchCandidate(task_id=tid, run_id=rid, source="DISCOVERY",
            family_id="theme_ppa_v3:nov", experiment_group=case["case_id"],
            expression=case["expression"], expression_hash="expr", settings_hash="settings",
            settings=settings, validation_status="VALID", request_status="UNKNOWN",
            gate_failures=[], metrics={}, result_snapshot={"research_case": case,
                "request": {"location": "/simulations/original"}}))
        await db.commit()
        brain = PollOnly()
        fields = {"dataset1": [{"id": name, "type": "MATRIX"} for name in base["datasets"]["dataset1"]]}
        result = await workflow.reconcile_paused(db, task, run, brain, fields, quality_evaluator=quality)
        assert result == {"resolved": 1, "pending": 0}
        row = (await db.execute(select(ResearchCandidate).where(ResearchCandidate.run_id == rid))).scalar_one()
        assert row.request_status == "RESULT"
        assert row.result_snapshot["ppa_evaluation"]["status"] == "PPA_REJECTED"
        assert run.block_reason == "PPA_BATCH_READY"
        await workflow.reconcile_paused(db, task, run, brain, fields, quality_evaluator=quality)
        assert brain.polls == 1


@pytest.mark.asyncio
async def test_paused_multi_child_recovers_by_parent_and_expression(async_engine, monkeypatch):
    maker, tid, rid, base = await setup(async_engine, monkeypatch)
    case = discovery()["cases"][0]
    case["epoch"] = 1
    settings = {**base["settings"], "region": "USA", "universe": "TOP3000", "delay": 1}
    class PollPair(Brain):
        async def poll_existing_simulation(self, location):
            pytest.fail("Parent multi-simulation requires child matching")
        async def poll_existing_multisim(self, location, expression):
            assert (location, expression) == ("/simulations/parent", case["expression"])
            return {"success": True, "alpha_id": "from-parent", "expression": expression,
                    "stage": "IS", "status": "UNSUBMITTED", "checks": [],
                    "metrics": {"sharpe": 0.8, "fitness": 0.4, "test_sharpe": 0.3,
                                "amer_sharpe": 0.7, "turnover": 0.2, "margin": 0.001,
                                "long_count": 100, "short_count": 100}}
    async def quality(**kwargs): return "OPTIMIZE"
    async with maker() as db:
        task, run = await db.get(MiningTask, tid), await db.get(ExperimentRun, rid)
        task.config = {**task.config, "research_protocol": "theme_ppa_v3", "continuous_mode": True,
                       "research_request_budget": None}
        db.add(ResearchCandidate(task_id=tid, run_id=rid, source="DISCOVERY",
            family_id="theme_ppa_v3:nov", experiment_group=case["case_id"],
            expression=case["expression"], expression_hash="expr", settings_hash="settings",
            settings=settings, validation_status="VALID", request_status="UNKNOWN",
            gate_failures=[], metrics={}, result_snapshot={"research_case": case,
                "request": {"location": "/simulations/parent", "transport": "paired_multi"}}))
        await db.commit()
        fields = {"dataset1": [{"id": name, "type": "MATRIX"} for name in base["datasets"]["dataset1"]]}
        result = await workflow.reconcile_paused(db, task, run, PollPair(), fields,
                                                  quality_evaluator=quality)
        assert result == {"resolved": 1, "pending": 0}


@pytest.mark.asyncio
async def test_unknown_without_location_remains_charged_and_unpolled(async_engine, monkeypatch):
    maker, tid, rid, base = await setup(async_engine, monkeypatch)
    async with maker() as db:
        task, run = await db.get(MiningTask, tid), await db.get(ExperimentRun, rid)
        task.config = {**task.config, "research_protocol": "theme_ppa_v3", "continuous_mode": True}
        db.add(ResearchCandidate(task_id=tid, run_id=rid, source="DISCOVERY",
            family_id="theme_ppa_v3:nov", experiment_group="unknown", expression="rank(field_0_0)",
            expression_hash="expr", settings_hash="settings", settings={}, validation_status="VALID",
            request_status="UNKNOWN", gate_failures=[], metrics={}, result_snapshot={}))
        await db.commit()
        with pytest.raises(ValueError, match="PPA_RECOVERY_LOCATION_MISSING"):
            await workflow.reconcile_paused(db, task, run, Brain(), {}, quality_evaluator=None)
        assert (await db.execute(select(ResearchCandidate).where(
            ResearchCandidate.run_id == rid))).scalar_one().request_status == "UNKNOWN"


@pytest.mark.asyncio
async def test_pending_original_poll_stays_unknown(async_engine, monkeypatch):
    maker, tid, rid, base = await setup(async_engine, monkeypatch)
    class PendingBrain(Brain):
        async def poll_existing_simulation(self, location):
            assert location == "/simulations/ongoing"
            return {"status": "PENDING"}
        async def simulate_single(self, *args, **kwargs):
            pytest.fail("Never resubmit an unknown request")
    async with maker() as db:
        task, run = await db.get(MiningTask, tid), await db.get(ExperimentRun, rid)
        task.config = {**task.config, "research_protocol": "theme_ppa_v3", "continuous_mode": True}
        db.add(ResearchCandidate(task_id=tid, run_id=rid, source="DISCOVERY",
            family_id="theme_ppa_v3:nov", experiment_group="unknown", expression="rank(field_0_0)",
            expression_hash="expr", settings_hash="settings", settings={}, validation_status="VALID",
            request_status="UNKNOWN", gate_failures=[], metrics={}, result_snapshot={
                "request": {"location": "/simulations/ongoing"}}))
        await db.commit()
        assert await workflow.reconcile_paused(db, task, run, PendingBrain(), {}) == {
            "resolved": 0, "pending": 1}
        row = (await db.execute(select(ResearchCandidate).where(ResearchCandidate.run_id == rid))).scalar_one()
        assert row.request_status == "UNKNOWN"


@pytest.mark.asyncio
async def test_definitive_original_poll_error_is_failed(async_engine, monkeypatch):
    maker, tid, rid, _ = await setup(async_engine, monkeypatch)
    class FailedBrain(Brain):
        async def poll_existing_simulation(self, location):
            assert location == "/simulations/failed"
            return {"status": "ERROR"}
        async def simulate_single(self, *args, **kwargs):
            pytest.fail("Never resubmit an unknown request")
    async with maker() as db:
        task, run = await db.get(MiningTask, tid), await db.get(ExperimentRun, rid)
        task.config = {**task.config, "research_protocol": "theme_ppa_v3", "continuous_mode": True}
        db.add(ResearchCandidate(task_id=tid, run_id=rid, source="DISCOVERY",
            family_id="theme_ppa_v3:nov", experiment_group="failed", expression="rank(field_0_0)",
            expression_hash="expr", settings_hash="settings", settings={}, validation_status="VALID",
            request_status="UNKNOWN", gate_failures=[], metrics={}, result_snapshot={
                "request": {"location": "/simulations/failed"}}))
        await db.commit()
        assert await workflow.reconcile_paused(db, task, run, FailedBrain(), {}) == {
            "resolved": 1, "pending": 0}
        row = (await db.execute(select(ResearchCandidate).where(ResearchCandidate.run_id == rid))).scalar_one()
        assert row.request_status == "FAILED"


@pytest.mark.asyncio
async def test_unevaluated_result_blocks_new_simulation(async_engine, monkeypatch):
    maker, tid, rid, base = await setup(async_engine, monkeypatch)
    case = discovery()["cases"][0]
    async with maker() as db:
        task, run = await db.get(MiningTask, tid), await db.get(ExperimentRun, rid)
        task.config = {**task.config, "research_protocol": "theme_ppa_v3", "continuous_mode": True,
                       "research_request_budget": None}
        run.checkpoint = {}
        db.add(ResearchCandidate(task_id=tid, run_id=rid, source="DISCOVERY",
            family_id="theme_ppa_v3:nov", experiment_group="older", expression="rank(field_0_1)",
            expression_hash="expr", settings_hash="settings", settings={}, validation_status="VALID",
            request_status="RESULT", wq_alpha_id="older-alpha", gate_failures=[], metrics={},
            result_snapshot={"alpha_id": "older-alpha"}))
        await db.commit()
        context = await workflow.feedback_context(db, task, run)
        assert context["unresolved_requests"] == 1
        batch = freeze_batch(base, context, discovery())
        await workflow.import_batch(db, task, run, batch)
        task.status = run.status = "RUNNING"
        await db.commit()
        brain = Brain()
        fields = {"dataset1": [{"id": name, "type": "MATRIX"} for name in base["datasets"]["dataset1"]]}
        outcome = await workflow.run_theme_v2(db, brain, task, run, fields)
        assert outcome["reason"] == "PPA_REQUEST_RECONCILIATION_PENDING"
        assert brain.calls == 0
