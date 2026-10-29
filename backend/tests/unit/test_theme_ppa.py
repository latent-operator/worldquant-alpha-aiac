from types import SimpleNamespace

import pytest
from backend.models import MiningTask, ExperimentRun
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from unittest.mock import AsyncMock

from backend.services.theme_ppa import validate_theme_task, phase_cases, candidate_plan_hash


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
    frozen = {
        "theme_id": "theme-2026-11", "verified_source": "platform",
        "platform_source_url": "https://platform.worldquantbrain.com/power-pool/themes/theme-2026-11",
        "account_eligibility": {"verified_at": "2026-10-27T00:00:00Z",
                                "power_pool_access": True, "quota_remaining": 80},
        "starts_at": "2026-11-01T00:00:00Z", "ends_at": "2026-11-30T23:59:59Z",
        "region": "USA", "universe": "TOP3000", "delay": 1,
        "ppa_corr_max": 0.5, "turnover_min": 0.01,
        "turnover_max": 0.7, "min_positions": 10,
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
    frozen["candidate_generation"] = {
        "source": "cockpit_local_responses", "model": "gpt-6-luna",
        "prepared_at": "2026-10-27T00:00:00Z",
        "plan_sha256": candidate_plan_hash(frozen),
    }
    return frozen


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


def test_theme_protocol_rejects_invalid_turnover_bounds():
    bad = task()
    bad.config["ppa_manifest"]["turnover_min"] = 0.8
    assert validate_theme_task(bad) == "PPA_MANIFEST_INCOMPLETE"


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


def test_theme_protocol_accepts_account_confirmed_unlimited_simulation_quota():
    candidate = task()
    eligibility = candidate.config["ppa_manifest"]["account_eligibility"]
    eligibility.pop("quota_remaining")
    eligibility.update({"quota_mode": "NO_FIXED_LIMIT", "quota_source": "account_owner"})
    candidate.config["ppa_manifest"]["candidate_generation"]["plan_sha256"] = candidate_plan_hash(
        candidate.config["ppa_manifest"]
    )
    assert validate_theme_task(candidate) is None

    eligibility.pop("quota_source")
    assert validate_theme_task(candidate) == "PPA_ACCOUNT_UNVERIFIED"


def test_discovery_treatment_changes_one_expression_token():
    bad = task()
    bad.config["ppa_manifest"]["discovery"][1]["expression"] = "zscore(other_field)"
    assert validate_theme_task(bad) == "PPA_DISCOVERY_NOT_SINGLE_FACTOR"


def test_refinement_only_uses_positive_complete_paired_seed():
    m = manifest()
    m["refinement"] = [{"case_id": "r1", "parent_case_id": "t0_1",
                        "dataset_id": "dataset1", "expression": "rank(rank(field_0_1))"}]
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


def test_official_theme_name_allows_discovery_without_unpublished_machine_ids():
    candidate = task()
    frozen = candidate.config["ppa_manifest"]
    frozen.pop("theme_id")
    frozen["theme_name"] = "GLB/D1 Power Pool Nov'26"
    frozen["eligible_classification_ids"] = []
    frozen["candidate_generation"]["plan_sha256"] = candidate_plan_hash(frozen)
    assert validate_theme_task(candidate) is None

    frozen["theme_name"] = ""
    frozen["candidate_generation"]["plan_sha256"] = candidate_plan_hash(frozen)
    assert validate_theme_task(candidate) == "PPA_THEME_ID_MISSING"


@pytest.mark.asyncio
async def test_expired_theme_pauses_before_simulation(db_session):
    from backend.services.theme_ppa import run_theme_dispatch

    config = task().config
    config["ppa_manifest"]["starts_at"] = "2026-09-01T00:00:00Z"
    config["ppa_manifest"]["ends_at"] = "2026-10-01T00:00:00Z"
    config["ppa_manifest"]["candidate_generation"]["plan_sha256"] = candidate_plan_hash(
        config["ppa_manifest"])
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
async def test_theme_dispatch_does_not_count_discovery_as_robust_ppa_ready(async_engine, monkeypatch):
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
    m["candidate_generation"]["plan_sha256"] = candidate_plan_hash(m)
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
        assert outcome["reason"] is None
        assert outcome["ready"] == 0
        assert any(c.result_snapshot.get("platform_ppa_evaluation", {}).get("status") == "PPA_READY"
                   for c in candidates)
        assert all(c.result_snapshot.get("ppa_evaluation", {}).get("status") != "PPA_READY"
                   for c in candidates)
        assert all(a.quality_status == "OPTIMIZE" for a in alphas)


def test_robustness_requires_parent_and_two_confirmations_with_positive_metrics():
    from backend.services.theme_ppa import robust_parent_cases

    m = manifest()
    m["refinement"] = [{"case_id": "r1", "parent_case_id": "t0_1",
                        "dataset_id": "dataset1", "expression": "rank(field_0_1)"}]
    m["confirmation"] = [
        {"case_id": "k1", "parent_case_id": "r1", "dataset_id": "dataset1",
         "expression": "rank(rank(field_0_1))", "settings": {"decay": 2}},
        {"case_id": "k2", "parent_case_id": "r1", "dataset_id": "dataset1",
         "expression": "rank(rank(field_0_1))", "settings": {"decay": 8}},
    ]
    metrics = {"sharpe": 1.2, "fitness": 0.8, "two_year_sharpe": 0.7,
               "test_sharpe": 0.5}
    def row(case_id, *, status="PPA_READY", values=None):
        return SimpleNamespace(experiment_group=case_id, request_status="RESULT",
            metrics=values or metrics,
            result_snapshot={"platform_ppa_evaluation": {"status": status}})

    parent, first, second = row("r1"), row("k1"), row("k2")
    assert robust_parent_cases(m, [parent, first]) == []
    assert robust_parent_cases(m, [parent, first, second]) == ["r1"]
    second.result_snapshot["platform_ppa_evaluation"]["status"] = "PPA_PENDING"
    assert robust_parent_cases(m, [parent, first, second]) == []
    second.result_snapshot["platform_ppa_evaluation"]["status"] = "PPA_READY"
    second.metrics = {**metrics, "test_sharpe": -0.1}
    assert robust_parent_cases(m, [parent, first, second]) == []


@pytest.mark.asyncio
async def test_confirmation_upgrades_parent_only_after_both_platform_ready_probes(async_engine, monkeypatch):
    from backend.services.theme_ppa import run_theme_dispatch
    from backend.services import research_audit as audit_module
    from backend.agents import mining_agent as mining_module
    from backend.models import ResearchCandidate
    from sqlalchemy import select

    maker = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(audit_module, "AsyncSessionLocal", maker)
    m = manifest()
    m["starts_at"], m["ends_at"] = "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z"
    m["confirmation_settings"] = {"decay": [2, 8]}
    m["refinement"] = [{"case_id": "r1", "parent_case_id": "t0_1",
                        "dataset_id": "dataset1", "expression": "rank(rank(field_0_1))"}]
    m["confirmation"] = [
        {"case_id": "k1", "parent_case_id": "r1", "dataset_id": "dataset1",
         "expression": "rank(rank(field_0_1))", "settings": {"decay": 2}},
        {"case_id": "k2", "parent_case_id": "r1", "dataset_id": "dataset1",
         "expression": "rank(rank(field_0_1))", "settings": {"decay": 8}},
    ]
    m["candidate_generation"]["plan_sha256"] = candidate_plan_hash(m)
    metrics = {"sharpe": 1.3, "fitness": 0.8, "turnover": 0.2, "margin": 0.001,
               "two_year_sharpe": 0.8, "test_sharpe": 0.6,
               "long_count": 100, "short_count": 100}
    async with maker() as db:
        task_row = MiningTask(task_name="ppa-confirm", region="USA", universe="TOP3000",
                              dataset_strategy="SPECIFIC", target_datasets=["dataset1"],
                              status="RUNNING", config={"research_protocol": "theme_ppa_v1",
                                  "research_request_budget": 80, "ppa_manifest": m})
        db.add(task_row)
        await db.flush()
        run = ExperimentRun(task_id=task_row.id, status="RUNNING",
                            checkpoint={"ppa_phase": "confirmation", "ppa_cursor": 0},
                            config_snapshot={"task": {"config": task_row.config}})
        db.add(run)
        await db.flush()
        db.add(ResearchCandidate(
            task_id=task_row.id, run_id=run.id, family_id="theme", source="REFINEMENT",
            experiment_group="r1", expression="rank(rank(field_0_1))",
            expression_hash="parent", settings_hash="parent", settings={},
            validation_status="VALID", request_status="RESULT", gate_failures=[],
            metrics=metrics, result_snapshot={
                "platform_ppa_evaluation": {"status": "PPA_READY", "reasons": []},
                "ppa_evaluation": {"status": "PPA_PENDING", "reasons": ["ROBUSTNESS_REVIEW_PENDING"]},
            },
        ))
        await db.commit()

        class FakeAgent:
            def __init__(self, *_args):
                pass
            async def _quality_status_for_optimization_result(self, **_kwargs):
                return "OPTIMIZE"
        monkeypatch.setattr(mining_module, "MiningAgent", FakeAgent)

        class Brain:
            calls = 0
            async def simulate_single(self, _expression, **_settings):
                self.calls += 1
                return {"success": True, "alpha_id": f"WQK{self.calls}",
                        "stage": "IS", "status": "UNSUBMITTED", "platform_can_submit": True,
                        "classifications": [{"id": "POWER_POOL_ELIGIBLE"}],
                        "themes": [{"id": "theme-2026-11"}],
                        "checks": [{"name": "MATCHES_THEMES", "result": "PASS"}],
                        "metrics": metrics}
            async def check_power_pool_correlation(self, _alpha_id):
                return {"max": 0.2}

        brain = Brain()
        fields = {"dataset1": [{"id": f, "type": "MATRIX"}
                               for f in m["datasets"]["dataset1"]]}
        outcome = await run_theme_dispatch(db, brain, task_row, run, fields)
        parent = (await db.execute(select(ResearchCandidate).where(
            ResearchCandidate.experiment_group == "r1"))).scalar_one()
        assert brain.calls == 2
        assert outcome["reason"] == "PPA_READY"
        assert parent.result_snapshot["ppa_evaluation"]["status"] == "PPA_READY"
        assert parent.result_snapshot["robustness_review"]["confirmation_cases"] == ["k1", "k2"]
        from backend.services.research_report import candidate_public, research_report
        public = candidate_public(parent)
        assert public["platform_ppa_evaluation"]["status"] == "PPA_READY"
        assert public["robustness_review"]["status"] == "PASS"
        report = await research_report(db, run.id)
        assert report["robustness_reviews"][0]["confirmation_cases"] == ["k1", "k2"]


@pytest.mark.asyncio
async def test_theme_dispatch_pauses_after_one_rate_limited_request(async_engine, monkeypatch):
    from backend.services.theme_ppa import run_theme_dispatch
    from backend.services import research_audit as audit_module
    from backend.models import ResearchCandidate
    from sqlalchemy import select

    maker = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(audit_module, "AsyncSessionLocal", maker)
    m = manifest()
    m["starts_at"] = "2026-01-01T00:00:00Z"
    m["ends_at"] = "2027-01-01T00:00:00Z"
    m["candidate_generation"]["plan_sha256"] = candidate_plan_hash(m)
    async with maker() as db:
        mining_task = MiningTask(task_name="ppa-rate-limit", region="USA", universe="TOP3000",
                                 dataset_strategy="SPECIFIC", target_datasets=["dataset1"],
                                 status="RUNNING", config={"research_protocol": "theme_ppa_v1",
                                                           "research_request_budget": 80,
                                                           "ppa_manifest": m})
        db.add(mining_task)
        await db.flush()
        run = ExperimentRun(task_id=mining_task.id, status="RUNNING")
        db.add(run)
        await db.commit()

        class RateLimitedBrain:
            calls = 0

            async def simulate_single(self, _expression, **_settings):
                self.calls += 1
                return {"success": False, "http_status": 429,
                        "error": "Creation failed: THROTTLED"}

        brain = RateLimitedBrain()
        fields = {"dataset1": [{"id": f, "type": "MATRIX"}
                                for f in m["datasets"]["dataset1"]]}
        outcome = await run_theme_dispatch(db, brain, mining_task, run, fields)
        candidates = (await db.execute(select(ResearchCandidate))).scalars().all()
        assert brain.calls == 1
        assert len(candidates) == 1
        assert candidates[0].request_status == "FAILED"
        assert outcome == {"done": True, "reason": "PPA_RATE_LIMIT",
                           "phase": "discovery", "cursor": 1, "ready": 0}
        assert mining_task.status == run.status == "PAUSED"
        assert run.checkpoint["ppa_cursor"] == 1


@pytest.mark.asyncio
async def test_audited_single_simulation_reports_429_without_retry():
    from backend.adapters.brain_adapter import BrainAdapter

    adapter = BrainAdapter(email="test@example.invalid", password="test")
    adapter._safe_api_call = AsyncMock(return_value=SimpleNamespace(
        status_code=429, headers={"Retry-After": "60"}, text="THROTTLED"))
    result = await adapter.simulate_alpha("rank(field)", retry_post=False)
    assert result["http_status"] == 429
    assert result["retry_after"] == 60
    adapter._safe_api_call.assert_awaited_once()


def test_audit_snapshot_keeps_rate_limit_diagnostics():
    from backend.services.research_audit import safe_result

    snapshot = safe_result({"success": False, "http_status": 429,
                            "retry_after": 60.0, "error": "THROTTLED"})
    assert snapshot["http_status"] == 429
    assert snapshot["retry_after"] == 60.0
    assert "error" not in snapshot


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
