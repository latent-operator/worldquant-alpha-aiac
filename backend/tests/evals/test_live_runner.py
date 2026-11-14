"""Lab runner integration: all WQ behavior is replaced by a local fake."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
import pytest

from backend.models import Alpha, ExperimentRun, MiningTask, ResearchCandidate
from backend.services import research_audit
from backend.evals.live_runner import run_lab
from backend.evals.live_ppa import freeze_experiment
from backend.tests.evals.test_live_ppa import feedback


class FakeBrain:
    def __init__(self, result):
        self.result = result
        self.posts = 0
        self.polls = 0

    async def simulate_single(self, expression, **kwargs):
        self.posts += 1
        return dict(self.result)

    async def check_power_pool_correlation(self, alpha_id):
        return {"max": 0.2}

    async def poll_existing_simulation(self, location):
        self.polls += 1
        return {"error": "uncertain simulation still pending"}

    async def submit_alpha(self, *args, **kwargs):
        pytest.fail("Submission endpoint must never be used")


def manifest():
    data = feedback()
    base = data["base"]
    base.update(region="GLB", universe="TOP3000", delay=1,
                starts_at="2026-11-01T00:00:00Z", ends_at="2026-11-22T00:00:00Z",
                settings={"decay": 4, "neutralization": "SUBINDUSTRY",
                          "truncation": 0.08, "test_period": "P2Y0M"},
                turnover_max=0.7, min_positions=10, ppa_corr_max=0.5)
    return freeze_experiment(data, source_run_id=28, source_task_id=14,
                             prepare_commit="a" * 40, prepared_at="2026-11-13T00:00:00Z",
                             budget=3, top_k=1, min_exclusive=0)


async def quality_evaluator(**kwargs):
    return "OPTIMIZE"


def clear_state():
    return {"source_unresolved": 0, "other_active_work": False,
            "platform_verified": True}


@pytest.mark.asyncio
async def test_lab_run_persists_one_result_and_never_submits(async_engine, monkeypatch):
    maker = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(research_audit, "AsyncSessionLocal", maker)
    brain = FakeBrain({"success": True, "alpha_id": "A-LAB-1", "stage": "IS",
                       "status": "UNSUBMITTED", "metrics": {"sharpe": 1.2,
                           "fitness": 0.4, "test_sharpe": 0.2, "turnover": 0.1,
                           "margin": 0.002, "long_count": 100, "short_count": 100},
                       "checks": [], "classifications": [], "themes": []})
    output = await run_lab(manifest(), maker=maker, brain=brain,
                           live_state=clear_state, quality_evaluator=quality_evaluator,
                           max_steps=1)
    assert output["sent"] == 1 and brain.posts == 1
    async with maker() as db:
        rows = (await db.execute(select(ResearchCandidate))).scalars().all()
        alphas = (await db.execute(select(Alpha))).scalars().all()
        assert len(rows) == len(alphas) == 1
        assert rows[0].request_status == "RESULT" and rows[0].quality_status == "OPTIMIZE"
        assert rows[0].result_snapshot["ppa_evaluation"]["status"] != "PPA_READY"
        assert rows[0].result_snapshot["research_case"]["parent_source_candidate_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("result,expected_reason", [
    ({"error": "uncertain network result"}, "EVAL_REQUEST_RECONCILIATION_PENDING"),
    ({"error": "ConnectError: ConnectError('')"}, "EVAL_REQUEST_RECONCILIATION_PENDING"),
    ({"http_status": 429, "error": "rate limit"}, "EVAL_RATE_LIMIT"),
])
async def test_lab_stops_after_uncertain_or_rate_limited_request(async_engine, monkeypatch,
                                                                  result, expected_reason):
    maker = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(research_audit, "AsyncSessionLocal", maker)
    brain = FakeBrain(result)
    frozen = manifest()
    output = await run_lab(frozen, maker=maker, brain=brain,
                           live_state=clear_state, quality_evaluator=quality_evaluator)
    assert output["sent"] == 1 and brain.posts == 1
    async with maker() as db:
        run = (await db.execute(select(ExperimentRun))).scalar_one()
        task = (await db.execute(select(MiningTask))).scalar_one()
        assert run.block_reason == expected_reason and task.status == "PAUSED"
    if "uncertain" in result.get("error", ""):
        async def inconclusive_poll(location):
            brain.polls += 1
            return {"error": "404 not found"}
        brain.poll_existing_simulation = inconclusive_poll
        resumed = await run_lab(frozen, maker=maker, brain=brain,
                                live_state=clear_state, quality_evaluator=quality_evaluator)
        assert resumed["sent"] == 0 and brain.posts == 1
    if result.get("http_status") == 429:
        resumed = await run_lab(frozen, maker=maker, brain=brain,
                                live_state=clear_state, quality_evaluator=quality_evaluator)
        assert resumed["reason"] == "EVAL_RATE_LIMIT" and brain.posts == 1


@pytest.mark.asyncio
async def test_lab_pauses_when_source_state_check_fails_midrun(async_engine, monkeypatch):
    maker = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(research_audit, "AsyncSessionLocal", maker)
    brain = FakeBrain({"success": True, "alpha_id": "A-LAB-2", "stage": "IS",
                       "status": "UNSUBMITTED", "metrics": {"sharpe": 1.2,
                           "fitness": 0.4, "test_sharpe": 0.2, "turnover": 0.1,
                           "margin": 0.002, "long_count": 100, "short_count": 100},
                       "checks": [], "classifications": [], "themes": []})
    checks = 0

    def state():
        nonlocal checks
        checks += 1
        if checks == 3:
            raise RuntimeError("source unavailable")
        return clear_state()

    output = await run_lab(manifest(), maker=maker, brain=brain, live_state=state,
                           quality_evaluator=quality_evaluator)
    assert output["sent"] == 1 and brain.posts == 1
    assert output["reason"] == "EVAL_SOURCE_CHECK_FAILED"
    async with maker() as db:
        run = (await db.execute(select(ExperimentRun))).scalar_one()
        assert run.status == "PAUSED"
