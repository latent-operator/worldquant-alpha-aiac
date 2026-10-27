from types import SimpleNamespace

import pytest
from backend.models import MiningTask, ExperimentRun
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from unittest.mock import AsyncMock

from backend.services.theme_ppa import validate_theme_task, phase_cases


def manifest():
    discovery = []
    for pair in range(6):
        discovery.append({"case_id": f"c{pair}", "pair_id": f"p{pair}",
                          "arm": "CONTROL", "dataset_id": "dataset1",
                          "expression": f"rank(field_{pair}_0)"})
        for treatment in range(1, 4):
            discovery.append({"case_id": f"t{pair}_{treatment}", "pair_id": f"p{pair}",
                              "arm": "TREATMENT", "dataset_id": "dataset1",
                              "expression": f"rank(field_{pair}_{treatment})"})
    return {
        "theme_id": "theme-2026-11", "verified_source": "platform",
        "platform_source_url": "https://platform.worldquantbrain.com/power-pool/themes/theme-2026-11",
        "account_eligibility": {"verified_at": "2026-10-27T00:00:00Z",
                                "power_pool_access": True, "quota_remaining": 80},
        "starts_at": "2026-11-01T00:00:00Z", "ends_at": "2026-11-30T23:59:59Z",
        "region": "USA", "universe": "TOP3000", "delay": 1,
        "ppa_corr_max": 0.5, "turnover_max": 0.7, "min_positions": 10,
        "max_fields": 3, "max_operator_count": 8,
        "datasets": {"dataset1": [f"field_{p}_{t}" for p in range(6) for t in range(4)]},
        "dataset_categories": {"dataset1": "analyst"},
        "allowed_operators": ["rank"],
        "eligible_classification_ids": ["POWER_POOL_ELIGIBLE"],
        "required_pre_submit_checks": ["MATCHES_THEMES"],
        "settings": {"decay": 4, "neutralization": "SUBINDUSTRY",
                     "truncation": 0.08, "test_period": "P2Y0M"},
        "discovery": discovery, "refinement": [], "confirmation": [],
    }


def task():
    return SimpleNamespace(
        region="USA", universe="TOP3000", dataset_strategy="SPECIFIC",
        target_datasets=["dataset1"], config={"research_protocol": "theme_ppa_v1",
            "research_request_budget": 80, "ppa_manifest": manifest()},
    )


def test_theme_protocol_requires_verified_frozen_manifest_and_80_request_cap():
    assert validate_theme_task(task()) is None
    bad = task()
    bad.config["ppa_manifest"]["verified_source"] = "forum"
    assert validate_theme_task(bad) == "PPA_THEME_NOT_PLATFORM_VERIFIED"
    bad = task()
    bad.config["research_request_budget"] = 100
    assert validate_theme_task(bad) == "PPA_BUDGET_MUST_BE_80"


def test_theme_protocol_rejects_continuous_mode_and_phase_overrun():
    bad = task()
    bad.config["continuous_mode"] = True
    assert validate_theme_task(bad) == "PPA_CONTINUOUS_FORBIDDEN"
    bad = task()
    bad.config["ppa_manifest"]["refinement"] = [
        {"case_id": f"r{i}", "parent_case_id": "t0_1", "dataset_id": "dataset1",
         "expression": f"rank(field_{i % 6}_1)"} for i in range(41)]
    assert validate_theme_task(bad) == "PPA_PHASE_LIMIT"


def test_theme_protocol_rejects_case_scope_override_and_duplicate_requests():
    bad = task()
    bad.config["ppa_manifest"]["discovery"][0]["settings"] = {"region": "EUR"}
    assert validate_theme_task(bad) == "PPA_CASE_SETTINGS_INVALID"
    bad = task()
    bad.config["ppa_manifest"]["discovery"][1]["expression"] = "rank(field_0_0)"
    assert validate_theme_task(bad) == "PPA_DUPLICATE_CASE_REQUEST"


def test_theme_protocol_requires_account_access_and_operator_whitelist():
    bad = task()
    bad.config["ppa_manifest"]["account_eligibility"]["power_pool_access"] = False
    assert validate_theme_task(bad) == "PPA_ACCOUNT_UNVERIFIED"
    bad = task()
    bad.config["ppa_manifest"]["allowed_operators"] = []
    assert validate_theme_task(bad) == "PPA_MANIFEST_INCOMPLETE"


def test_refinement_only_uses_positive_complete_paired_seed():
    m = manifest()
    m["refinement"] = [{"case_id": "r1", "parent_case_id": "t0_1",
                        "dataset_id": "dataset1", "expression": "rank(field_0_1)"}]
    rows = [SimpleNamespace(expression="rank(field_0_0)", experiment_group="c0",
                            arm="CONTROL", pair_id="p0",
                            request_status="RESULT", metrics={"sharpe": 0.4, "fitness": 0.2,
                            "turnover": 0.2, "margin": 0.001}),
            SimpleNamespace(expression="rank(field_0_1)", experiment_group="t0_1",
                            arm="TREATMENT", pair_id="p0",
                            request_status="RESULT", metrics={"sharpe": 0.5, "fitness": 0.3,
                            "turnover": 0.2, "margin": 0.001})]
    assert [c["case_id"] for c in phase_cases(m, "refinement", rows)] == ["r1"]
    rows[1].metrics["fitness"] = 0.1
    assert phase_cases(m, "refinement", rows) == []


def test_unconfirmed_theme_never_produces_ready_cases():
    bad = task()
    bad.config["ppa_manifest"]["theme_id"] = ""
    assert validate_theme_task(bad) == "PPA_THEME_ID_MISSING"


@pytest.mark.asyncio
async def test_expired_theme_pauses_before_simulation(db_session):
    from backend.services.theme_ppa import run_theme_dispatch

    config = task().config
    config["ppa_manifest"]["starts_at"] = "2026-09-01T00:00:00Z"
    config["ppa_manifest"]["ends_at"] = "2026-10-01T00:00:00Z"
    mining_task = MiningTask(task_name="ppa", region="USA", universe="TOP3000",
                             dataset_strategy="SPECIFIC", target_datasets=["dataset1"],
                             status="RUNNING", config=config)
    db_session.add(mining_task)
    await db_session.flush()
    run = ExperimentRun(task_id=mining_task.id, status="RUNNING")
    db_session.add(run)
    await db_session.commit()

    class Brain:
        calls = 0

        async def simulate_batch(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("expired theme must never simulate")

    brain = Brain()
    outcome = await run_theme_dispatch(db_session, brain, mining_task, run, {})
    assert outcome["reason"] == "PPA_THEME_ENDED"
    assert mining_task.status == "PAUSED"
    assert brain.calls == 0


@pytest.mark.asyncio
async def test_theme_dispatch_persists_platform_ready_separately_from_strict_quality(async_engine, monkeypatch):
    from backend.services.theme_ppa import run_theme_dispatch
    from backend.models import ResearchCandidate, Alpha
    from backend.services import research_audit as audit_module
    from backend.agents import mining_agent as mining_module
    from sqlalchemy import select

    maker = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(audit_module, "AsyncSessionLocal", maker)
    m = manifest()
    m["starts_at"] = "2026-01-01T00:00:00Z"
    m["ends_at"] = "2027-01-01T00:00:00Z"
    cfg = {"research_protocol": "theme_ppa_v1", "research_request_budget": 80,
           "ppa_manifest": m}
    async with maker() as db:
        mining_task = MiningTask(task_name="ppa", region="USA", universe="TOP3000",
                                 dataset_strategy="SPECIFIC", target_datasets=["dataset1"],
                                 status="RUNNING", config=cfg)
        db.add(mining_task)
        await db.flush()
        run = ExperimentRun(task_id=mining_task.id, status="RUNNING")
        db.add(run)
        await db.commit()

        class FakeAgent:
            def __init__(self, *_args):
                pass

            async def _quality_status_for_optimization_result(self, **_kwargs):
                return "OPTIMIZE"

        monkeypatch.setattr(mining_module, "MiningAgent", FakeAgent)

        class Brain:
            calls = 0

            async def simulate_batch(self, expressions, **_settings):
                start = self.calls
                self.calls += len(expressions)
                return [{"success": True, "alpha_id": f"WQ{i}",
                         "stage": "IS", "status": "UNSUBMITTED", "can_submit": True,
                         "platform_can_submit": True,
                         "classifications": [{"name": "POWER_POOL", "value": "POWER_POOL_ELIGIBLE"}],
                         "themes": [{"id": "theme-2026-11"}],
                         "checks": [{"name": "MATCHES_THEMES", "result": "PASS"}],
                         "metrics": {"sharpe": 1.3, "fitness": 0.8, "turnover": 0.2,
                                     "margin": 0.001, "long_count": 100, "short_count": 100,
                                     "two_year_sharpe": 0.8,
                                     "test_sharpe": 0.6}}
                        for i, _ in enumerate(expressions, start=start)]

            async def check_power_pool_correlation(self, _alpha_id):
                return {"max": 0.2}

        fields = {"dataset1": [{"id": f, "type": "MATRIX"} for f in m["datasets"]["dataset1"]]}
        outcome = await run_theme_dispatch(db, Brain(), mining_task, run, fields)
        candidates = (await db.execute(select(ResearchCandidate))).scalars().all()
        alphas = (await db.execute(select(Alpha))).scalars().all()
        assert outcome["reason"] == "PPA_READY"
        assert any(c.result_snapshot.get("ppa_evaluation", {}).get("status") == "PPA_READY"
                   for c in candidates)
        assert all(a.quality_status == "OPTIMIZE" for a in alphas)


@pytest.mark.asyncio
async def test_create_task_rejects_unverified_theme(db_session):
    from backend.services.task_service import TaskService, TaskCreateData

    m = manifest()
    m["verified_source"] = "forum"
    data = TaskCreateData(name="unverified", region="USA", universe="TOP3000",
                          dataset_strategy="SPECIFIC", target_datasets=["dataset1"],
                          config={"research_protocol": "theme_ppa_v1",
                                  "research_request_budget": 80, "ppa_manifest": m})
    with pytest.raises(ValueError, match="PPA_THEME_NOT_PLATFORM_VERIFIED"):
        await TaskService(db_session).create_task(data)


@pytest.mark.asyncio
async def test_theme_report_separates_ppa_ready_from_strict_quality(db_session):
    from backend.models import ResearchCandidate, Alpha
    from backend.services.research_report import research_report

    m = manifest()
    task_row = MiningTask(task_name="report", region="USA", universe="TOP3000",
                          status="PAUSED", config={"research_protocol": "theme_ppa_v1",
                                                   "research_request_budget": 80,
                                                   "ppa_manifest": m})
    db_session.add(task_row)
    await db_session.flush()
    run = ExperimentRun(task_id=task_row.id, status="PAUSED",
                        config_snapshot={"task": {"config": task_row.config}})
    db_session.add(run)
    await db_session.flush()
    db_session.add(ResearchCandidate(
        task_id=task_row.id, run_id=run.id, family_id="theme", source="DISCOVERY",
        expression="rank(x)", expression_hash="h1", settings_hash="h2", settings={},
        validation_status="VALID", request_status="RESULT", gate_failures=[], metrics={},
        result_snapshot={"ppa_evaluation": {"status": "PPA_READY", "reasons": []}},
    ))
    db_session.add(Alpha(task_id=task_row.id, run_id=run.id, alpha_id="WQ-READY",
                         expression="rank(x)", region="USA", universe="TOP3000",
                         quality_status="OPTIMIZE", is_sharpe=1.2, is_fitness=0.6))
    await db_session.commit()
    report = await research_report(db_session, run.id)
    assert report["ppa_counts"] == {"PPA_READY": 1}
    assert report["candidate_funnel"]["quality_pass"] == 0
    assert report["budget"]["limit"] == 80
    assert report["budget"]["remaining"] == 79


def test_worker_routes_theme_protocol_without_generic_mining(monkeypatch):
    from backend.tasks import mining_tasks
    from backend.services import theme_ppa

    task_row = SimpleNamespace(id=2, status="RUNNING", region="USA", universe="TOP3000",
                               target_datasets=["dataset1"], config={"research_protocol": "theme_ppa_v1"})
    run = SimpleNamespace(id=3, task_id=2, status="RUNNING", celery_task_id=None, checkpoint={})
    db = SimpleNamespace(execute=AsyncMock(side_effect=[
        SimpleNamespace(scalar_one_or_none=lambda: task_row), SimpleNamespace(rowcount=1)]),
        get=AsyncMock(return_value=run), commit=AsyncMock(), refresh=AsyncMock(),
        rollback=AsyncMock())

    class Session:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_):
            return False

    class Brain:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    dispatch = AsyncMock(return_value={"done": True, "reason": "PPA_READY"})
    monkeypatch.setattr(mining_tasks, "AsyncSessionLocal", Session)
    monkeypatch.setattr(mining_tasks, "MCPBrainAdapter", Brain)
    monkeypatch.setattr(mining_tasks, "_get_or_create_run", AsyncMock(return_value=run))
    monkeypatch.setattr(mining_tasks, "_get_dataset_fields", AsyncMock(return_value=[{"id": "field_0_0"}]))
    monkeypatch.setattr(theme_ppa, "run_theme_dispatch", dispatch)
    monkeypatch.setattr("backend.alpha_semantic_validator.load_operators_from_db",
                        AsyncMock(return_value=[]))

    result = mining_tasks.run_mining_task.run(2, 3)
    assert result["research_reason"] == "PPA_READY"
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_ppa_80_request_boundary_counts_unknown_and_failure(async_engine, monkeypatch):
    from backend.services import research_audit

    maker = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(research_audit, "AsyncSessionLocal", maker)
    async with maker() as db:
        task_row = MiningTask(task_name="bounded", region="USA", universe="TOP3000",
                              status="RUNNING")
        db.add(task_row)
        await db.flush()
        run = ExperimentRun(task_id=task_row.id, status="RUNNING")
        db.add(run)
        await db.commit()
        task_id, run_id = task_row.id, run.id
    ledger = research_audit.ResearchLedger(task_id, run_id, max_requests=80)
    for index in range(80):
        candidate, reason = await ledger.reserve(f"rank(field_{index})", {"delay": 1}, "DISCOVERY")
        assert candidate is not None and reason is None
        await ledger.finish(candidate, {"success": False,
                                        "error": "timed out" if index % 2 else "bad response"})
    candidate, reason = await ledger.reserve("rank(field_80)", {"delay": 1}, "CONFIRMATION")
    assert candidate is None and reason == "BUDGET_EXHAUSTED"
