"""Isolated continuous discovery; every external service is replaced locally."""
from copy import deepcopy

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from backend.models import ExperimentRun, ResearchCandidate
from backend.services import research_audit
from backend.tests.evals.test_live_runner import FakeBrain, clear_state, quality_evaluator
from backend.tests.unit.test_ppa_feedback import base as fixture_base
from backend.evals.discovery_campaign import choose_dataset, freeze_campaign_batch, run_campaign_batch


def campaign_and_batch():
    base = fixture_base()
    base.update(region="GLB", universe="TOP3000", delay=1,
                starts_at="2026-11-01T00:00:00Z", ends_at="2026-11-22T00:00:00Z",
                settings={"decay": 4, "neutralization": "SUBINDUSTRY",
                          "truncation": 0.08, "test_period": "P2Y0M"},
                turnover_max=0.7, min_positions=10, ppa_corr_max=0.5)
    campaign = {"campaign_id": "new-dataset-test", "base": base,
                "dataset_id": "pv1", "source_task_id": 14, "source_run_id": 28,
                "code_commit": "a" * 40, "old_lab_task_id": 1}
    evidence = {"next_phase": "discovery", "phase_limit": 16,
                "phase_counts": {"discovery": 0}, "max_batch_cases": 16,
                "budget_remaining": None, "continuous": True,
                "rows": [], "seeds": [], "history_signatures": [],
                "history_behavioral_signatures": [], "exhausted_templates": [],
                "evidence_hash": "frozen", "strict_max_operator_count": 5}
    cases = [{"case_id": f"case_{window}", "pair_id": "group_1",
              "arm": "CONTROL" if index == 0 else "TREATMENT",
              "dataset_id": "pv1", "mechanism_id": "reversal",
              "expression": f"rank(ts_sum(returns,{window}))",
              "changed_element": "lookback", "expected_effect": "measure signal",
              "risk": "weak signal", "stop_condition": "no improvement"}
             for index, window in enumerate((5, 10, 20, 30))]
    batch = freeze_campaign_batch(campaign, evidence,
                                  {"phase": "discovery", "cases": cases}, index=1)
    return campaign, batch


def test_dataset_selection_requires_new_live_matrix_fields():
    datasets = [{"id": "sentiment26", "category": "sentiment", "coverage": 1.0},
                {"id": "news76", "category": "news", "coverage": 1.0}]
    fields = {"sentiment26": [{"id": "vector_only", "type": "VECTOR", "coverage": 0.8}],
              "news76": [{"id": "a", "type": "MATRIX", "coverage": 0.8,
                          "dateCoverage": 0.8},
                         {"id": "b", "type": "MATRIX", "coverage": 0.8,
                          "dateCoverage": 0.8}]}
    choice = choose_dataset(datasets, fields, excluded={"pv1", "model110", "predictive_starmine"})
    assert choice["dataset_id"] == "news76"
    assert choice["field_ids"] == ["a", "b"]


def test_batch_is_frozen_and_rejects_tampering():
    campaign, batch = campaign_and_batch()
    assert len(batch["cases"]) == 4
    assert batch["batch_sha256"]
    changed = deepcopy(batch)
    changed["cases"][0]["expression"] = "rank(close)"
    from backend.evals.discovery_campaign import validate_campaign_batch
    with pytest.raises(ValueError, match="BATCH_HASH_MISMATCH"):
        validate_campaign_batch(campaign, changed)


@pytest.mark.asyncio
@pytest.mark.parametrize("result,reason", [
    ({"success": False, "error_code": "TRANSPORT_UNCERTAIN"}, "EVAL_REQUEST_RECONCILIATION_PENDING"),
    ({"success": False, "http_status": 429, "error_code": "RATE_LIMITED", "retry_after": 30}, "EVAL_RATE_LIMIT"),
])
async def test_campaign_stops_after_first_uncertain_or_429(async_engine, monkeypatch, result, reason):
    maker = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(research_audit, "AsyncSessionLocal", maker)
    campaign, batch = campaign_and_batch()
    brain = FakeBrain(result)
    output = await run_campaign_batch(campaign, batch, maker=maker, brain=brain,
                                      live_state=clear_state, quality_evaluator=quality_evaluator)
    assert output["sent"] == brain.posts == 1
    assert output["reason"] == reason
    async with maker() as db:
        run = (await db.execute(select(ExperimentRun))).scalar_one()
        rows = (await db.execute(select(ResearchCandidate))).scalars().all()
        assert run.config_snapshot["task"]["config"]["research_request_budget"] is None
        assert len(rows) == 1
        if reason == "EVAL_REQUEST_RECONCILIATION_PENDING":
            assert rows[0].request_status == "UNKNOWN"


@pytest.mark.asyncio
async def test_campaign_resumes_same_batch_without_duplicate_post(async_engine, monkeypatch):
    maker = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(research_audit, "AsyncSessionLocal", maker)
    campaign, batch = campaign_and_batch()
    brain = FakeBrain({"success": True, "alpha_id": "CAMPAIGN-A", "metrics": {
        "sharpe": 0.5, "fitness": 0.2, "turnover": 0.1, "margin": 0.0002,
        "long_count": 100, "short_count": 100}, "checks": []})
    first = await run_campaign_batch(campaign, batch, maker=maker, brain=brain,
                                     live_state=clear_state, quality_evaluator=quality_evaluator,
                                     max_steps=1)
    second = await run_campaign_batch(campaign, batch, maker=maker, brain=brain,
                                      live_state=clear_state, quality_evaluator=quality_evaluator,
                                      max_steps=1)
    assert first["sent"] == second["sent"] == 1
    assert brain.posts == 2
    async with maker() as db:
        runs = (await db.execute(select(ExperimentRun))).scalars().all()
        assert len(runs) == 1


@pytest.mark.asyncio
async def test_campaign_qualifies_persisted_result_before_next_post(async_engine, monkeypatch):
    maker = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(research_audit, "AsyncSessionLocal", maker)
    campaign, batch = campaign_and_batch()
    brain = FakeBrain({"success": True, "alpha_id": "CAMPAIGN-A", "metrics": {
        "sharpe": 0.5, "fitness": 0.2, "turnover": 0.1, "margin": 0.0002,
        "long_count": 100, "short_count": 100}, "checks": []})
    from backend.evals import discovery_campaign as module
    original = module.persist_result
    async def interrupted(*args, **kwargs):
        raise RuntimeError("interrupted after result ledger commit")
    monkeypatch.setattr(module, "persist_result", interrupted)
    with pytest.raises(RuntimeError):
        await run_campaign_batch(campaign, batch, maker=maker, brain=brain,
                                 live_state=clear_state, quality_evaluator=quality_evaluator)
    assert brain.posts == 1
    monkeypatch.setattr(module, "persist_result", original)
    recovered = await run_campaign_batch(campaign, batch, maker=maker, brain=brain,
                                         live_state=clear_state, quality_evaluator=quality_evaluator,
                                         max_steps=1)
    assert recovered["sent"] == 1
    assert brain.posts == 2
    async with maker() as db:
        rows = (await db.execute(select(ResearchCandidate).order_by(ResearchCandidate.id))).scalars().all()
        assert rows[0].result_snapshot["ppa_evaluation"]


@pytest.mark.asyncio
async def test_rate_limit_review_never_auto_resumes(async_engine, monkeypatch):
    maker = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(research_audit, "AsyncSessionLocal", maker)
    campaign, batch = campaign_and_batch()
    brain = FakeBrain({"success": False, "http_status": 429, "error_code": "RATE_LIMITED",
                       "retry_after": 1})
    for _ in range(3):
        result = await run_campaign_batch(campaign, batch, maker=maker, brain=brain,
                                          live_state=clear_state, quality_evaluator=quality_evaluator)
        if result["reason"] == "EVAL_RATE_LIMIT":
            async with maker() as db:
                run = (await db.execute(select(ExperimentRun))).scalar_one()
                run.next_retry_at = None
                await db.commit()
    assert result["reason"] == "EVAL_RATE_LIMIT_REVIEW"
    repeated = await run_campaign_batch(campaign, batch, maker=maker, brain=brain,
                                        live_state=clear_state, quality_evaluator=quality_evaluator)
    assert repeated["reason"] == "EVAL_RATE_LIMIT_REVIEW"
    assert brain.posts == 3
