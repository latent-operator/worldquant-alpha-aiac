from copy import deepcopy
from types import SimpleNamespace
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
