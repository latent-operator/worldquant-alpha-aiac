from types import SimpleNamespace
from backend.services.theme_ppa import validate_theme_task
from backend.services.ppa_feedback import build_feedback, freeze_batch
from backend.tests.unit.test_theme_ppa import manifest
from backend.tests.unit.test_ppa_feedback import base
from scripts.watch_theme_ppa import first_batch_reviewable
from scripts import watch_theme_ppa
import pytest
from backend.models import ExperimentRun, MiningTask
from backend.services import research_audit as audit
from backend.tests.unit.test_ppa_recovery import campaign
from backend.tests.unit.test_ppa_v2_workflow import setup, discovery, Brain
from backend.services import theme_ppa_v2 as workflow
from backend.services.research_report import research_report


def test_continuous_theme_allows_no_total_request_cap():
    data = manifest()
    data.update(mechanisms={"m": {"dataset_id": "dataset1", "hypothesis": "signal", "direction": "positive"}})
    task = SimpleNamespace(region=data["region"], universe=data["universe"], dataset_strategy="SPECIFIC",
                           target_datasets=list(data["datasets"]), config={"research_protocol": "theme_ppa_v3",
                           "continuous_mode": True, "research_request_budget": None,
                           "budget_root_run_id": 16, "ppa_manifest": data})
    assert validate_theme_task(task) is None


def test_continuous_feedback_can_restart_discovery_after_exhausted_epoch():
    exhausted = build_feedback([], None, {"discovery": 12, "refinement": 12, "confirmation": 12},
                               continuous=True)
    assert exhausted["next_phase"] is None
    restarted = build_feedback([], None, {}, continuous=True)
    assert restarted["next_phase"] == "discovery"
    assert restarted["budget_remaining"] is None


def test_discovery_continues_when_first_four_have_no_seed():
    evidence = build_feedback([], None, {"discovery": 4}, continuous=True)
    assert evidence["next_phase"] == "discovery"


def test_discovery_with_two_slots_left_rolls_epoch_before_cockpit():
    evidence = build_feedback([], None, {"discovery": 10}, continuous=True)
    assert evidence["next_phase"] is None
    assert evidence["stop_reason"] == "PPA_DISCOVERY_GROUP_CAPACITY"


def test_no_budget_limit_does_not_skip_two_probe_rule():
    evidence = build_feedback([], None, {}, continuous=True)
    data = base()
    data["datasets"]["pv1"] = ["returns", "adv20", "close", "open"]
    proposal = {"phase": "discovery", "cases": [dict(case_id=f"c{i}", pair_id="p1",
        arm="CONTROL" if i == 0 else "TREATMENT", dataset_id="pv1",
        mechanism_id="reversal", expression=f"rank({field})", changed_element="field",
        expected_effect="improve AMER", risk="noise", stop_condition="no gain")
        for i, field in enumerate(data["datasets"]["pv1"])]}
    batch = freeze_batch(data, evidence, proposal)
    assert len(batch["cases"]) == 4


def test_first_batch_review_requires_complete_platform_evidence():
    rows = [{"run_id": 17, "source": "DISCOVERY", "request_status": "RESULT",
             "metrics": {"amer_sharpe": 0.8}, "platform_ppa_evaluation": {"status": "PPA_REJECTED"}}
            for _ in range(4)]
    assert first_batch_reviewable({"rows": rows}, 17)
    rows[2]["platform_ppa_evaluation"]["status"] = "PPA_PENDING"
    assert not first_batch_reviewable({"rows": rows}, 17)
    rows[2]["platform_ppa_evaluation"]["status"] = "PPA_REJECTED"
    rows[3]["metrics"] = {}
    assert not first_batch_reviewable({"rows": rows}, 17)


@pytest.mark.asyncio
async def test_continuous_successor_can_reserve_after_historical_eighty(async_engine, monkeypatch):
    maker, tasks, runs = await campaign(async_engine, monkeypatch)
    old = audit.ResearchLedger(tasks[0], runs[0], 80)
    for index in range(80):
        candidate_id, reason = await old.reserve(f"rank(f{index})", {}, "DISCOVERY")
        assert candidate_id and reason is None
    async with maker() as db:
        run = await db.get(ExperimentRun, runs[1])
        own = dict(run.config_snapshot)
        own["task"] = {"config": {"research_protocol": "theme_ppa_v3", "ppa_manifest": {"theme_id": "nov"}}}
        run.config_snapshot = own
        await db.commit()
    new = audit.ResearchLedger(tasks[1], runs[1], None, budget_root_run_id=runs[0])
    candidate_id, reason = await new.reserve("rank(fresh)", {}, "DISCOVERY")
    assert candidate_id and reason is None


@pytest.mark.asyncio
async def test_continuous_run_saves_first_batch_without_eighty_cap(async_engine, monkeypatch):
    maker, task_id, run_id, data = await setup(async_engine, monkeypatch)
    async def quality(**kwargs):
        return "OPTIMIZE"
    async with maker() as db:
        task, run = await db.get(MiningTask, task_id), await db.get(ExperimentRun, run_id)
        task.config = {**task.config, "research_protocol": "theme_ppa_v3",
                       "research_request_budget": None, "continuous_mode": True}
        run.config_snapshot = {"task": {"config": task.config}}
        await db.commit()
        evidence = await workflow.feedback_context(db, task, run)
        assert evidence["budget_remaining"] is None
        assert evidence["max_batch_cases"] == 4
        batch = freeze_batch(data, evidence, discovery())
        await workflow.import_batch(db, task, run, batch)
        task.status = run.status = "RUNNING"
        await db.commit()
        fields = {"dataset1": [{"id": name, "type": "MATRIX"} for name in data["datasets"]["dataset1"]]}
        result = await workflow.run_theme_v2(db, Brain(), task, run, fields, quality_evaluator=quality)
        assert result["reason"] == "PPA_FIRST_BATCH_REVIEW"
        report = await research_report(db, run_id)
        assert report["budget"] == {"limit": None, "remaining": None, "reserved": 4}


@pytest.mark.asyncio
async def test_next_epoch_discovers_when_only_seed_has_unknown_mechanism(async_engine, monkeypatch):
    maker, task_id, run_id, _ = await setup(async_engine, monkeypatch)
    async with maker() as db:
        task, run = await db.get(MiningTask, task_id), await db.get(ExperimentRun, run_id)
        task.config = {**task.config, "research_protocol": "theme_ppa_v3", "continuous_mode": True,
                       "research_request_budget": None}
        run.checkpoint = {"ppa_epoch": 1}
        await db.commit()
        monkeypatch.setattr(workflow, "build_feedback", lambda *args, **kwargs: {
            "rows": [], "seeds": [{"mechanism_id": "unmapped"}], "finalists": [],
            "stopped_families": [], "phase_counts": {}, "next_phase": "refinement",
            "stop_reason": None, "evidence_hash": "stub"})
        evidence = await workflow.feedback_context(db, task, run)
        assert evidence["next_phase"] == "discovery"
        assert evidence["stop_reason"] is None


@pytest.mark.asyncio
async def test_continuous_phase_slots_include_cached_and_unknown(async_engine, monkeypatch):
    from backend.models import ResearchCandidate
    maker, task_id, run_id, data = await setup(async_engine, monkeypatch)
    async with maker() as db:
        task, run = await db.get(MiningTask, task_id), await db.get(ExperimentRun, run_id)
        task.config = {**task.config, "research_protocol": "theme_ppa_v3", "continuous_mode": True,
                       "research_request_budget": None}
        run.checkpoint = {"ppa_epoch": 1}
        for index, status in enumerate(("RESULT", "CACHED", "UNKNOWN", "FAILED")):
            db.add(ResearchCandidate(task_id=task_id, run_id=run_id, source="DISCOVERY",
                family_id="theme_ppa_v3:nov", experiment_group=f"case{index}",
                expression=f"rank(field_0_{index})", expression_hash=f"expr{index}",
                settings_hash="settings", settings={}, validation_status="VALID",
                request_status=status, gate_failures=[], metrics={},
                result_snapshot={"research_case": {"epoch": 1}}))
        await db.commit()
        context = await workflow.feedback_context(db, task, run)
        assert context["phase_counts"]["discovery"] == 4
        assert context["request_phase_counts"]["discovery"] == 3
        assert len(context["history_signatures"]) == 4


@pytest.mark.asyncio
async def test_epoch_advance_is_atomic_and_stale_retry_is_rejected(async_engine, monkeypatch):
    maker, task_id, run_id, data = await setup(async_engine, monkeypatch)
    async with maker() as db:
        task, run = await db.get(MiningTask, task_id), await db.get(ExperimentRun, run_id)
        task.config = {**task.config, "research_protocol": "theme_ppa_v3", "continuous_mode": True,
                       "research_request_budget": None}
        run.checkpoint = {"ppa_epoch": 1, "ppa_batch": {"cases": []}, "ppa_batch_cursor": 0}
        from backend.models import ResearchCandidate
        for index in range(16):
            db.add(ResearchCandidate(task_id=task_id, run_id=run_id, source="DISCOVERY",
                family_id="theme_ppa_v3:nov", experiment_group=f"legacy{index}",
                expression=f"rank(field_0_{index})", expression_hash=f"expr{index}",
                settings_hash="settings", settings={}, validation_status="VALID",
                request_status="RESULT", gate_failures=[], metrics={},
                result_snapshot={"research_case": {"epoch": 1},
                                 "ppa_evaluation": {"status": "PPA_REJECTED"}}))
        await db.commit()
        result = await workflow.advance_epoch(db, task, run, expected_epoch=1)
        assert result["epoch"] == 2
        with pytest.raises(ValueError, match="STALE_EPOCH"):
            await workflow.advance_epoch(db, task, run, expected_epoch=1)


def test_watcher_advances_exhausted_epoch_before_generation(tmp_path, monkeypatch):
    calls = []
    feedback = iter([{"epoch": 1, "next_phase": None, "stop_reason": "PPA_DISCOVERY_GROUP_CAPACITY",
                      "continuous": True}, {"epoch": 2, "next_phase": "discovery", "base": {}}])
    def request(url, payload=None):
        calls.append((url, payload))
        if url.endswith("/tasks/14"):
            return {"status": "PAUSED"}
        if url.endswith("/tasks/14/runs"):
            return [{"id": 18, "status": "PAUSED", "block_reason": "AWAITING_RESEARCH_BATCH"}]
        if url.endswith("/ppa-feedback"):
            return next(feedback)
        return {"ok": True}
    monkeypatch.setattr(watch_theme_ppa, "request_json", request)
    monkeypatch.setattr(watch_theme_ppa, "prepare_batch", lambda *args: {
        "phase": "discovery", "cases": [], "candidate_generation": {"plan_sha256": "abc"}})
    result = watch_theme_ppa.step("http://127.0.0.1:8003/api/v1", 14, 18, tmp_path / "ask.ps1", tmp_path)
    assert result.startswith("IMPORTED:")
    assert [url for url, _ in calls if url.endswith("/ppa-epoch")]
    assert next(i for i, (url, _) in enumerate(calls) if url.endswith("/ppa-epoch")) < next(
        i for i, (url, _) in enumerate(calls) if url.endswith("/ppa-batch"))


def test_watcher_reconciles_unknown_without_generating(tmp_path, monkeypatch):
    calls = []
    def request(url, payload=None):
        calls.append((url, payload))
        if url.endswith("/tasks/14"):
            return {"status": "PAUSED"}
        if url.endswith("/tasks/14/runs"):
            return [{"id": 18, "status": "PAUSED", "block_reason": "AWAITING_RESEARCH_BATCH"}]
        if url.endswith("/ppa-feedback"):
            return {"unresolved_requests": 1, "next_phase": "discovery", "epoch": 2}
        if url.endswith("/ppa-reconcile"):
            return {"resolved": 0, "pending": 1}
        pytest.fail(f"Unexpected request: {url}")
    monkeypatch.setattr(watch_theme_ppa, "request_json", request)
    monkeypatch.setattr(watch_theme_ppa, "prepare_batch", lambda *args: pytest.fail("No batch yet"))
    assert watch_theme_ppa.step("http://127.0.0.1:8003/api/v1", 14, 18,
                                tmp_path / "ask.ps1", tmp_path) == "WAIT_RECONCILE"
    assert any(url.endswith("/ppa-reconcile") for url, _ in calls)
