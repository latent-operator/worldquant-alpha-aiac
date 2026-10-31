"""Quota waiting must not turn provider failures into fake WQ candidates."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.agents.services.llm_service import LLMBlockedError, LLMService, classify_llm_error
from backend.agents.graph.state import AlphaCandidate, MiningState
from backend.agents.graph.nodes import generation
from backend.agents.graph.nodes.evaluation import node_simulate
from backend.agents.graph.nodes.persistence import node_save_results
from backend.tasks.mining_tasks import _next_wait_minutes, _wait_is_due
from backend.tasks import mining_tasks
from backend.services.task_service import TaskService
from backend.services.run_service import RunService
from backend.routers.runs import ExperimentRunDetailResponse
from backend.services.dashboard_service import DashboardService
from backend.agents.mining_agent import _next_no_new_streak
from backend.celery_app import _recover_unfinished_runs, celery_app


class ProviderError(Exception):
    def __init__(self, status_code, message):
        self.status_code = status_code
        super().__init__(message)


@pytest.mark.parametrize("error, expected", [
    (ProviderError(403, "daily usage limit exceeded sk-secret"), "DAILY_QUOTA"),
    (ProviderError(401, "invalid api key sk-secret"), "INVALID_CREDENTIALS"),
    (ProviderError(429, "rate limit"), "RATE_LIMIT"),
    (ConnectionError("cannot connect"), "CONNECTION_ERROR"),
])
def test_provider_failure_categories_do_not_contain_key(error, expected):
    assert classify_llm_error(error) == expected
    assert "sk-secret" not in str(LLMBlockedError(expected))


@pytest.mark.asyncio
async def test_quota_fails_before_template_or_wq(monkeypatch):
    service = LLMService(api_key="sk-test-key", base_url="https://example.invalid/v1", model="MiniMax-M2.5")
    service._ensure_credentials_loaded = AsyncMock()
    service.client.chat.completions.create = AsyncMock(side_effect=ProviderError(403, "daily usage limit exceeded sk-test-key"))
    with pytest.raises(LLMBlockedError, match="DAILY_QUOTA"):
        await service.call("system", "user")
    assert service.client.chat.completions.create.await_count == 1

    templates = Mock()
    monkeypatch.setattr(generation, "_augment_with_template_candidates", templates)
    state = MiningState(task_id=3, dataset_id="news", fields=[{"id": "news_field", "type": "MATRIX"}])
    blocked_llm = SimpleNamespace(call=AsyncMock(side_effect=LLMBlockedError("DAILY_QUOTA")))
    with pytest.raises(LLMBlockedError):
        await generation.node_code_gen(state, blocked_llm)
    templates.assert_not_called()


@pytest.mark.asyncio
async def test_generated_candidates_exclude_prior_failed_expression():
    state = MiningState(task_id=3, dataset_id="news", fields=[{"id": "x", "type": "MATRIX"}])
    llm = SimpleNamespace(call=AsyncMock(return_value=SimpleNamespace(
        success=True,
        parsed={"alphas": [
            {"expression": "rank(x)"},
            {"expression": "rank(y)"},
            {"expression": "rank(y)"},
        ]},
        error=None,
    )))
    result = await generation.node_code_gen(
        state, llm,
        {"configurable": {"task_config": {
            "attempted_expressions": ["rank(x)"],
            "generation_candidate_pool": 0,
        }}},
    )
    assert [candidate.expression for candidate in result["pending_alphas"]] == ["rank(y)"]


@pytest.mark.asyncio
async def test_transient_retry_really_runs_three_attempts(monkeypatch):
    service = LLMService(api_key="sk-test-key", base_url="https://example.invalid/v1", model="MiniMax-M2.5")
    service._ensure_credentials_loaded = AsyncMock()
    service.client.chat.completions.create = AsyncMock(side_effect=ProviderError(429, "rate limited"))
    monkeypatch.setattr("backend.agents.services.llm_service.asyncio.sleep", AsyncMock())
    with pytest.raises(LLMBlockedError, match="RATE_LIMIT"):
        await service.call("system", "user")
    assert service.client.chat.completions.create.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("error, kind, attempts", [
    (ProviderError(401, "invalid api key sk-test-key"), "INVALID_CREDENTIALS", 1),
    (ConnectionError("offline"), "CONNECTION_ERROR", 3),
])
async def test_auth_and_network_failures_have_distinct_retry_policy(monkeypatch, error, kind, attempts):
    service = LLMService(api_key="sk-test-key", base_url="https://example.invalid/v1", model="MiniMax-M2.5")
    service._ensure_credentials_loaded = AsyncMock()
    service.client.chat.completions.create = AsyncMock(side_effect=error)
    monkeypatch.setattr("backend.agents.services.llm_service.asyncio.sleep", AsyncMock())
    with pytest.raises(LLMBlockedError, match=kind):
        await service.call("system", "user")
    assert service.client.chat.completions.create.await_count == attempts


@pytest.mark.asyncio
async def test_duplicate_candidate_skips_wq_and_failure(monkeypatch):
    class SessionFactory:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_):
            return False

    monkeypatch.setattr("backend.database.AsyncSessionLocal", SessionFactory)
    monkeypatch.setattr("backend.selection_strategy.filter_unsimulated_expressions", AsyncMock(return_value=([], ["rank(x)"])))
    brain = SimpleNamespace(simulate_batch=AsyncMock())
    state = MiningState(task_id=3, pending_alphas=[AlphaCandidate(expression="rank(x)", is_valid=True)])
    simulated = await node_simulate(state, brain)
    assert simulated["pending_alphas"] == []
    brain.simulate_batch.assert_not_awaited()
    state.pending_alphas = simulated["pending_alphas"]
    saved = await node_save_results(state)
    assert saved["failures"] == []


def test_quota_backoff_and_revision_wakeup():
    assert [_next_wait_minutes("DAILY_QUOTA", i) for i in range(5)] == [15, 30, 60, 60, 60]
    assert _next_wait_minutes("INVALID_CREDENTIALS", 0) is None
    assert _next_wait_minutes("RATE_LIMIT", 0) == 5
    due = datetime.now(timezone.utc) + timedelta(hours=1)
    assert not _wait_is_due(due, 4, 4)
    assert _wait_is_due(due, 4, 5)
    assert not _wait_is_due(None, 4, 4)


def test_malformed_output_gets_fast_bounded_backoff():
    for kind in ("MALFORMED_HYPOTHESIS", "MALFORMED_RESPONSE"):
        assert [mining_tasks._next_wait_seconds(kind, n) for n in range(7)] == [
            30, 120, 300, 900, 1800, 3600, 3600,
        ]
    assert mining_tasks._next_wait_seconds("DAILY_QUOTA", 0) == 900
    assert mining_tasks._next_wait_seconds("NO_NEW_CANDIDATES", 0) == 300
    assert mining_tasks._next_wait_seconds("INVALID_CREDENTIALS", 0) is None


@pytest.mark.asyncio
async def test_connection_failure_tracks_safe_type_and_separate_bounded_backoff(monkeypatch):
    service = LLMService(api_key="sk-test-key", base_url="https://example.invalid/v1", model="MiniMax-M2.5")
    service._ensure_credentials_loaded = AsyncMock()
    service.client.chat.completions.create = AsyncMock(side_effect=TimeoutError("private sk-test-key"))
    monkeypatch.setattr("backend.agents.services.llm_service.asyncio.sleep", AsyncMock())
    with pytest.raises(LLMBlockedError) as raised:
        await service.call("system", "user")
    assert raised.value.kind == "CONNECTION_ERROR"
    assert raised.value.safe_detail == "TIMEOUT"
    assert "sk-test-key" not in str(raised.value)

    task = SimpleNamespace(id=10, status="RUNNING")
    run = SimpleNamespace(id=13, status="RUNNING", checkpoint={"iteration": 8},
                          block_reason=None, next_retry_at=None)
    db = SimpleNamespace(commit=AsyncMock())
    expected = [30, 120, 300, 900, 900]
    for attempt, delay in enumerate(expected, start=1):
        before = datetime.now(timezone.utc)
        await mining_tasks._schedule_llm_wait(
            db, task, run, "CONNECTION_ERROR", revision=2, safe_detail=raised.value.safe_detail,
        )
        actual = (run.next_retry_at - before).total_seconds()
        assert delay <= actual <= delay + 2
        assert run.checkpoint["connection_attempts"] == attempt
        assert run.checkpoint["connection_error_type"] == "TIMEOUT"
        assert run.block_reason == "CONNECTION_ERROR"

    await mining_tasks._schedule_llm_wait(db, task, run, "NO_NEW_CANDIDATES", revision=2)
    assert run.checkpoint["connection_attempts"] == 0
    assert run.checkpoint["connection_error_type"] is None
    assert mining_tasks._next_wait_seconds("DAILY_QUOTA", 0) == 900


def test_retry_poll_follows_short_deadline_and_caps_long_wait():
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    assert mining_tasks._retry_poll_seconds(now + timedelta(seconds=30), now=now) == 30
    assert mining_tasks._retry_poll_seconds(now + timedelta(minutes=2), now=now) == 120
    assert mining_tasks._retry_poll_seconds(now + timedelta(hours=1), now=now) == 300
    assert mining_tasks._retry_poll_seconds(None, now=now) == 300


def test_celery_retry_uses_short_wait_deadline(monkeypatch):
    def completed_run(coroutine):
        coroutine.close()
        return {"_retry": True, "run_id": 13, "retry_countdown": 30}

    retry = Mock(return_value=RuntimeError("scheduled"))
    monkeypatch.setattr(mining_tasks, "run_async", completed_run)
    monkeypatch.setattr(mining_tasks.run_mining_task, "retry", retry)

    with pytest.raises(RuntimeError, match="scheduled"):
        mining_tasks.run_mining_task.run(10, 13)

    retry.assert_called_once_with(countdown=30, args=(10, 13), max_retries=None)


def test_three_generation_flows_without_novel_candidate_reach_pause_threshold():
    streak = 0
    for expected in (1, 2, 3):
        streak = _next_no_new_streak(streak, 0)
        assert streak == expected
    assert streak >= 3
    assert _next_no_new_streak(streak, 1) == 0


@pytest.mark.asyncio
async def test_pause_then_resume_waiting_task_reuses_run(monkeypatch):
    run = SimpleNamespace(id=9, status="WAITING_LLM", block_reason="DAILY_QUOTA",
                          next_retry_at=datetime.now(timezone.utc), celery_task_id="old")
    task = SimpleNamespace(id=3, status="WAITING_LLM")
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: run)),
        commit=AsyncMock(), rollback=AsyncMock(),
    )
    service = TaskService(db)
    service.task_repo.get_by_id = AsyncMock(return_value=task)
    service.task_repo.update_status = AsyncMock()
    paused = await service.intervene_task(3, "PAUSE")
    assert paused["new_status"] == "PAUSED"
    assert run.status == "PAUSED" and run.next_retry_at is None

    task.status = "PAUSED"
    queued = Mock(return_value=SimpleNamespace(id="new-dispatch"))
    monkeypatch.setattr("backend.tasks.run_mining_task.delay", queued)
    resumed = await service.intervene_task(3, "RESUME")
    assert resumed["run_id"] == 9
    queued.assert_called_once_with(3, 9)
    assert run.celery_task_id == "new-dispatch"


@pytest.mark.asyncio
async def test_pause_then_resume_running_task_reuses_run(monkeypatch):
    run = SimpleNamespace(id=13, status="RUNNING", block_reason=None,
                          next_retry_at=None, celery_task_id="old-dispatch")
    task = SimpleNamespace(id=10, status="RUNNING")
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: run)),
        commit=AsyncMock(), rollback=AsyncMock(),
    )
    service = TaskService(db)
    service.task_repo.get_by_id = AsyncMock(return_value=task)
    service.task_repo.update_status = AsyncMock()

    paused = await service.intervene_task(10, "PAUSE")
    assert paused["new_status"] == "PAUSED"
    assert run.status == "PAUSED"

    task.status = "PAUSED"
    queued = Mock(return_value=SimpleNamespace(id="new-dispatch"))
    monkeypatch.setattr("backend.tasks.run_mining_task.delay", queued)
    resumed = await service.intervene_task(10, "RESUME")
    assert resumed["run_id"] == 13
    assert run.celery_task_id == "new-dispatch"
    queued.assert_called_once_with(10, 13)


@pytest.mark.asyncio
async def test_resuming_ppa_run_records_paused_time(monkeypatch):
    began = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    run = SimpleNamespace(id=18, status="PAUSED", next_retry_at=None,
                          checkpoint={"ppa_pause_started_at": began, "ppa_pause_seconds": 5},
                          celery_task_id="old")
    task = SimpleNamespace(id=14, status="PAUSED", config={"research_protocol": "theme_ppa_v3"})
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        scalar_one_or_none=lambda: run)), commit=AsyncMock(), rollback=AsyncMock())
    service = TaskService(db)
    service.task_repo.get_by_id = AsyncMock(return_value=task)
    service.task_repo.update_status = AsyncMock()
    monkeypatch.setattr("backend.tasks.run_mining_task.delay",
                        Mock(return_value=SimpleNamespace(id="new")))
    await service.intervene_task(14, "RESUME")
    assert 65 <= run.checkpoint["ppa_pause_seconds"] < 70
    assert "ppa_pause_started_at" not in run.checkpoint


@pytest.mark.asyncio
async def test_stop_cancels_waiting_run():
    run = SimpleNamespace(id=9, status="WAITING_LLM", next_retry_at=datetime.now(timezone.utc), finished_at=None)
    task = SimpleNamespace(id=3, status="WAITING_LLM")
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: run)),
        commit=AsyncMock(), rollback=AsyncMock(),
    )
    service = TaskService(db)
    service.task_repo.get_by_id = AsyncMock(return_value=task)
    service.task_repo.update_status = AsyncMock()
    stopped = await service.intervene_task(3, "STOP")
    assert stopped["new_status"] == "STOPPED"
    assert run.status == "STOPPED" and run.next_retry_at is None


@pytest.mark.asyncio
async def test_run_detail_exposes_safe_wait_state():
    due = datetime.now(timezone.utc) + timedelta(minutes=15)
    row = SimpleNamespace(
        id=5, task_id=3, status="WAITING_LLM", trigger_source="API", celery_task_id="worker-id",
        config_snapshot={}, prompt_version=None, thresholds_version=None, strategy_snapshot={},
        started_at=datetime.now(timezone.utc), finished_at=None, error_message=None,
        block_reason="DAILY_QUOTA", next_retry_at=due,
        checkpoint={"dataset_id": "news", "iteration": 0, "credential_revision": 2},
    )
    service = RunService(SimpleNamespace())
    service.run_repo.get_by_id = AsyncMock(return_value=row)
    info = await service.get_run(5)
    response = ExperimentRunDetailResponse.model_validate(info, from_attributes=True)
    assert response.block_reason == "DAILY_QUOTA"
    assert response.next_retry_at == due
    assert "api_key" not in response.model_dump_json().lower()


@pytest.mark.asyncio
async def test_kpi_separates_simulation_attempts_from_wq_results():
    def scalar_rows(rows):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    db = SimpleNamespace(execute=AsyncMock(side_effect=[
        scalar_rows([{"simulated_count": 4}, {"simulated_count": 3}, {"simulated_count": 0}]),
        SimpleNamespace(scalar=lambda: 0),
        scalar_rows([]),
        SimpleNamespace(scalar=lambda: 0),
    ]))
    service = DashboardService(db)
    service._count_alphas_in_range = AsyncMock(return_value=5)
    kpi = await service.get_kpi_metrics()
    assert kpi.today_simulation_candidates == 7
    assert kpi.today_wq_results == kpi.today_simulations == 5
    assert kpi.today_pass_count == 0
    assert kpi.today_avg_sharpe is None


@pytest.mark.asyncio
async def test_worker_startup_requeues_wait_without_creating_a_new_run(monkeypatch):
    run = SimpleNamespace(id=5, task_id=3, status="WAITING_LLM", celery_task_id="old-dispatch")

    class WaitingSession:
        def __init__(self):
            self.execute = AsyncMock(return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [run])
            ))
            self.commit = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    db = WaitingSession()
    monkeypatch.setattr("backend.database.AsyncSessionLocal", lambda: db)
    sent = Mock()
    monkeypatch.setattr(celery_app, "send_task", sent)
    assert await _recover_unfinished_runs() == 1
    assert run.id == 5 and run.task_id == 3
    assert run.celery_task_id != "old-dispatch"
    sent.assert_called_once_with(
        "backend.tasks.run_mining_task", args=[3, 5], task_id=run.celery_task_id,
    )


@pytest.mark.asyncio
async def test_worker_startup_also_selects_running_run_for_recovery(monkeypatch):
    run = SimpleNamespace(id=13, task_id=10, status="RUNNING", celery_task_id="old-dispatch")

    class Session:
        execute = AsyncMock(return_value=SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [run])
        ))
        commit = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    db = Session()
    monkeypatch.setattr("backend.database.AsyncSessionLocal", lambda: db)
    sent = Mock()
    monkeypatch.setattr(celery_app, "send_task", sent)

    assert await _recover_unfinished_runs() == 1
    query = str(db.execute.await_args.args[0].compile(compile_kwargs={"literal_binds": True}))
    assert "RUNNING" in query and "WAITING_LLM" in query
    assert "mining_tasks.status = experiment_runs.status" in query
    sent.assert_called_once_with(
        "backend.tasks.run_mining_task", args=[10, 13], task_id=run.celery_task_id,
    )
