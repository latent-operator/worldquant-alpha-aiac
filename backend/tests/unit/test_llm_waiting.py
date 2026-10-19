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
from backend.services.task_service import TaskService
from backend.services.run_service import RunService
from backend.routers.runs import ExperimentRunDetailResponse
from backend.services.dashboard_service import DashboardService
from backend.agents.mining_agent import _next_no_new_streak
from backend.celery_app import _recover_waiting_llm_runs, celery_app


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
    assert await _recover_waiting_llm_runs() == 1
    assert run.id == 5 and run.task_id == 3
    assert run.celery_task_id != "old-dispatch"
    sent.assert_called_once_with(
        "backend.tasks.run_mining_task", args=[3, 5], task_id=run.celery_task_id,
    )
