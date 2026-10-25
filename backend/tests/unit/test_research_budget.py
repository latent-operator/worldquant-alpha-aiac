import pytest
from types import SimpleNamespace
import httpx
from unittest.mock import AsyncMock
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from backend.adapters.brain_adapter import BrainAdapter
from backend.models import MiningTask, ExperimentRun
import backend.services.research_audit as audit_module
from backend.services.research_audit import ResearchBudgetBrain, paper_matrix, paired_improvements, constrained_mutations, safe_result
from backend.services.paper_experiment import APPROVED_FIELDS, validate_protocol_task
from backend.services.paper_experiment import run_paper_dispatch
from backend.services.research_report import paired_deltas


def test_paper_matrix_uses_shared_controls_and_approved_fields():
    cases = paper_matrix()
    assert len(cases) == 25
    assert sum(case["arm"] == "CONTROL" for case in cases) == 5
    assert sum(case["arm"] == "TREATMENT" for case in cases) == 20
    assert {case["window"] for case in cases} == {5, 10, 20, 30, 60}
    assert all("nws17_d1_event_sentiment_score" in case["expression"] for case in cases)


@pytest.mark.asyncio
async def test_budget_wrapper_sends_only_reserved_candidates_and_never_retries_unknown():
    calls = []

    class FakeLedger:
        async def reserve(self, expression, settings, source, metadata):
            return (len(calls) + 1, None) if expression != "duplicate" else (None, "DUPLICATE")

        async def finish(self, candidate_id, result):
            calls.append((candidate_id, result))

    class FakeBrain:
        async def simulate_batch(self, expressions, **settings):
            assert expressions == ["candidate"]
            return [{"success": False, "error": "timeout"}]

    wrapped = ResearchBudgetBrain(FakeBrain(), FakeLedger())
    result = await wrapped.simulate_batch(["duplicate", "candidate"], region="USA")
    assert result[0]["error"] == "DUPLICATE"
    assert result[1]["success"] is False
    assert len(calls) == 1


def test_seed_requires_complete_metrics_and_both_paired_gains():
    rows = [
        {"arm": "CONTROL", "pair_id": "window_5", "group": "event_sentiment",
         "metrics": {"sharpe": 0.4, "fitness": 0.2, "turnover": 0.1, "margin": 0.002}},
        {"arm": "TREATMENT", "pair_id": "window_5", "group": "event_relevance",
         "metrics": {"sharpe": 0.6, "fitness": 0.3, "turnover": 0.1, "margin": 0.002}},
        {"arm": "TREATMENT", "pair_id": "window_5", "group": "event_density",
         "metrics": {"sharpe": 0.7, "fitness": None, "turnover": 0.1, "margin": 0.002}},
    ]
    seeds = paired_improvements(rows)
    assert len(seeds) == 1
    assert seeds[0]["group"] == "event_relevance"
    assert len(constrained_mutations(seeds, limit=15)) <= 15


def test_paper_protocol_rejects_wrong_data_boundary_before_wq():
    task = SimpleNamespace(
        region="USA", universe="TOP3000", dataset_strategy="SPECIFIC",
        target_datasets=["news17"],
        config={"delay": 1, "research_only_fields": list(APPROVED_FIELDS),
                "research_request_budget": 40},
    )
    fields = [{"id": field, "type": "VECTOR"} for field in APPROVED_FIELDS]
    assert validate_protocol_task(task, fields) is None
    task.config["delay"] = 0
    assert validate_protocol_task(task, fields) == "PAPER_REGION_UNIVERSE_DELAY_MISMATCH"


def test_report_pairs_keep_missing_os_deltas_as_missing():
    control = SimpleNamespace(id=1, arm="CONTROL", pair_id="window_5",
                              request_status="RESULT", metrics={"sharpe": 0.5, "fitness": 0.2},
                              experiment_group="event_sentiment", window=5, wq_alpha_id="A",
                              quality_status="FAIL", gate_failures=[])
    treatment = SimpleNamespace(id=2, arm="TREATMENT", pair_id="window_5",
                                request_status="RESULT", metrics={"sharpe": 0.7, "fitness": 0.3},
                                experiment_group="event_relevance", window=5, wq_alpha_id="B",
                                quality_status="OPTIMIZE", gate_failures=["LOW_FITNESS"])
    row = paired_deltas([control, treatment])[0]
    assert round(row["deltas"]["sharpe"], 3) == 0.2
    assert row["deltas"]["os_sharpe"] is None
    assert row["quality_status"] == "OPTIMIZE"


def test_result_snapshot_drops_untrusted_text_and_nonfinite_metrics():
    secret = "sk-" + "x" * 32
    snapshot = safe_result({
        "success": True, "alpha_id": "WQ1",
        "metrics": {"sharpe": secret, "fitness": float("nan")},
        "checks": [{"name": secret, "result": "FAIL", "value": secret}],
        "raw_response": secret,
    })
    assert secret not in str(snapshot)
    assert snapshot["metrics"]["sharpe"] is None


def test_result_snapshot_keeps_verified_wq_stage_and_status_for_recovery():
    snapshot = safe_result({
        "success": True, "alpha_id": "WQ1", "stage": "IS",
        "status": "UNSUBMITTED", "metrics": {"sharpe": 0.8},
    })
    assert snapshot["stage"] == "IS"
    assert snapshot["status"] == "UNSUBMITTED"
    assert safe_result({"stage": "unknown", "status": "secret"})["stage"] is None
    assert safe_result({"stage": "unknown", "status": "secret"})["status"] is None


@pytest.mark.asyncio
async def test_audited_single_post_does_not_retry_timeout():
    class FakeClient:
        calls = 0

        async def post(self, *args, **kwargs):
            self.calls += 1
            raise httpx.ReadTimeout("unknown response")

    adapter = BrainAdapter.__new__(BrainAdapter)
    adapter.client = FakeClient()
    with pytest.raises(httpx.ReadTimeout):
        await adapter._safe_api_call("post", "/simulations", retry_writes=False, json={})
    assert adapter.client.calls == 1


@pytest.mark.asyncio
async def test_audited_batch_post_disables_retry_in_fallback():
    adapter = BrainAdapter.__new__(BrainAdapter)
    adapter._safe_api_call = AsyncMock(return_value=httpx.Response(503, text="unavailable"))
    results = await adapter.simulate_batch(["rank(a)", "rank(b)"], retry_post=False)
    assert len(results) == 2
    assert adapter._safe_api_call.await_args.kwargs["retry_writes"] is False


@pytest.mark.asyncio
async def test_ledger_reservation_survives_new_instance_and_counts_unknown(async_engine, monkeypatch):
    maker = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(audit_module, "AsyncSessionLocal", maker)
    async with maker() as db:
        task = MiningTask(task_name="audit", region="USA", universe="TOP3000", status="RUNNING")
        db.add(task)
        await db.flush()
        run = ExperimentRun(task_id=task.id, status="RUNNING")
        db.add(run)
        await db.commit()
        task_id, run_id = task.id, run.id

    ledger = audit_module.ResearchLedger(task_id, run_id, max_requests=1)
    first, reason = await ledger.reserve("rank(a)", {"delay": 1}, "GENERATION")
    assert first is not None and reason is None
    await ledger.mark_requested(first)
    await ledger.finish(first, {"success": False, "error": "timed out"})
    resumed = audit_module.ResearchLedger(task_id, run_id, max_requests=1)
    duplicate, duplicate_reason = await resumed.reserve("rank(a)", {"delay": 1}, "GENERATION")
    second, budget_reason = await resumed.reserve("rank(b)", {"delay": 1}, "GENETIC")
    assert duplicate is None and duplicate_reason == "DUPLICATE"
    assert second is None and budget_reason == "BUDGET_EXHAUSTED"


@pytest.mark.asyncio
async def test_paper_dispatch_keeps_one_run_and_saves_audited_wq_results(async_engine, monkeypatch):
    from sqlalchemy import func, select
    from backend.models import Alpha, ResearchCandidate
    from backend.agents import mining_agent as mining_module

    maker = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(audit_module, "AsyncSessionLocal", maker)

    class FakeAgent:
        def __init__(self, db, brain):
            pass

        async def _quality_status_for_optimization_result(self, **kwargs):
            assert kwargs["metrics"]["stage"] == "IS"
            assert kwargs["metrics"]["status"] == "UNSUBMITTED"
            kwargs["metrics"]["_strict_gate_failures"] = ["LOW_SHARPE"]
            return "OPTIMIZE"

    monkeypatch.setattr(mining_module, "MiningAgent", FakeAgent)

    class FakeBrain:
        calls = 0

        async def simulate_batch(self, expressions, **settings):
            self.calls += len(expressions)
            return [{"success": True, "alpha_id": f"TEST{self.calls}-{index}",
                     "stage": "IS", "status": "UNSUBMITTED",
                     "metrics": {"sharpe": 0.5, "fitness": 0.3, "turnover": 0.1,
                                 "margin": 0.002}, "checks": []}
                    for index, _ in enumerate(expressions)]

    config = {"research_protocol": "paper_news17_ablation_v1", "delay": 1,
              "research_only_fields": list(APPROVED_FIELDS), "research_request_budget": 40,
              "max_operator_count": 5}
    fields = [{"id": field, "type": "VECTOR"} for field in APPROVED_FIELDS]
    brain = FakeBrain()
    async with maker() as db:
        task = MiningTask(task_name="paper", region="USA", universe="TOP3000",
                          dataset_strategy="SPECIFIC", target_datasets=["news17"],
                          status="RUNNING", config=config, progress_current=0)
        db.add(task)
        await db.flush()
        run = ExperimentRun(task_id=task.id, status="RUNNING", checkpoint={})
        db.add(run)
        await db.commit()
        first = await run_paper_dispatch(db, brain, task, run, fields)
        second = await run_paper_dispatch(db, brain, task, run, fields)
        assert first["cursor"] == 5 and second["cursor"] == 10
        assert brain.calls == 10
        assert run.id == first.get("run_id", run.id)
        assert (await db.execute(select(func.count(Alpha.id)))).scalar_one() == 10
        assert (await db.execute(select(func.count(ResearchCandidate.id)).where(
            ResearchCandidate.request_status == "RESULT"))).scalar_one() == 10
