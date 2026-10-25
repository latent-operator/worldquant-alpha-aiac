"""A Celery dispatch must yield after one round without changing run accounting."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.agents import mining_agent
from backend.tasks import mining_tasks


def test_resumed_slice_uses_absolute_goal_but_counts_only_new_passes():
    checkpoint = {"dataset_id": "news17", "iteration": 8, "total_success": 2}
    prior, absolute_target = mining_tasks._slice_goal_and_prior_success(checkpoint, "news17", 3)
    assert (prior, absolute_target) == (2, 5)
    assert mining_tasks._new_passes_in_slice({"total_success": 3}, prior) == 1
    assert mining_tasks._new_passes_in_slice({"total_success": 2}, prior) == 0
    assert mining_tasks._slice_goal_and_prior_success(checkpoint, "other_dataset", 3) == (0, 3)


def test_one_completed_round_yields_unless_goal_or_iteration_limit_reached():
    base = dict(
        current_iteration=9, entry_iteration=8, max_iterations=100000,
        max_rounds=1, total_success=0, target_alphas=1000,
        min_iterations_before_goal_stop=1,
    )
    assert mining_agent._should_yield_dispatch_round(**base) is True
    assert mining_agent._should_yield_dispatch_round(**{**base, "current_iteration": 8}) is False
    assert mining_agent._should_yield_dispatch_round(**{**base, "max_iterations": 9}) is False
    assert mining_agent._should_yield_dispatch_round(**{**base, "total_success": 1000}) is False
    assert mining_agent._should_yield_dispatch_round(**{
        **base, "total_success": 1000, "min_iterations_before_goal_stop": 12,
    }) is True


def test_continuous_round_yields_even_after_legacy_goal_and_iteration_limit():
    assert mining_agent._should_yield_dispatch_round(
        current_iteration=10, entry_iteration=9, max_iterations=10,
        max_rounds=1, total_success=4, target_alphas=4,
        min_iterations_before_goal_stop=1, continuous_mode=True,
    ) is True


@pytest.mark.asyncio
async def test_continuous_new_candidate_count_uses_audited_wq_requests(db_session):
    from backend.models import ExperimentRun, MiningTask, ResearchCandidate
    task = MiningTask(task_name="audit count", region="USA", universe="TOP3000")
    db_session.add(task)
    await db_session.flush()
    run = ExperimentRun(task_id=task.id, status="RUNNING")
    db_session.add(run)
    await db_session.flush()
    db_session.add_all([
        ResearchCandidate(task_id=task.id, run_id=run.id, family_id="paper",
                          source="GENERATION", expression="rank(a)", expression_hash="a",
                          settings_hash="s", settings={}, validation_status="REJECTED",
                          request_status="NOT_REQUESTED", gate_failures=[], metrics={}, result_snapshot={}),
        ResearchCandidate(task_id=task.id, run_id=run.id, family_id="paper",
                          source="GENERATION", expression="rank(b)", expression_hash="b",
                          settings_hash="s", settings={}, validation_status="VALID",
                          request_status="UNKNOWN", gate_failures=[], metrics={}, result_snapshot={}),
    ])
    await db_session.commit()
    assert await mining_agent._audited_request_count(db_session, run.id) == 1


def test_worker_requeues_same_run_after_one_round_and_adds_only_delta(monkeypatch):
    task = SimpleNamespace(
        id=10, status="RUNNING", region="USA", universe="TOP3000",
        progress_current=2, daily_goal=5, max_iterations=100000, config={},
    )
    run = SimpleNamespace(
        id=13, task_id=10, status="RUNNING", celery_task_id=None,
        checkpoint={"dataset_id": "news17", "iteration": 8, "total_success": 2},
    )
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[
            SimpleNamespace(scalar_one_or_none=lambda: task),
            SimpleNamespace(rowcount=1),
        ]),
        get=AsyncMock(return_value=run),
        commit=AsyncMock(), refresh=AsyncMock(), rollback=AsyncMock(),
    )

    class SessionFactory:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_):
            return False

    class Brain:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    evolve = AsyncMock(return_value={
        "total_success": 3, "new_success": 1, "all_alphas": [],
        "yielded_for_dispatch": True, "iterations_completed": 9,
    })
    monkeypatch.setattr(mining_tasks, "AsyncSessionLocal", SessionFactory)
    monkeypatch.setattr(mining_tasks, "MCPBrainAdapter", Brain)
    monkeypatch.setattr(mining_tasks, "MiningAgent", lambda *_: SimpleNamespace(run_evolution_loop=evolve))
    monkeypatch.setattr(mining_tasks, "_get_or_create_run", AsyncMock(return_value=run))
    monkeypatch.setattr(mining_tasks, "_get_datasets_to_mine", AsyncMock(return_value=["news17"]))
    monkeypatch.setattr(mining_tasks, "_get_dataset_fields", AsyncMock(return_value=[{"id": "news_field"}]))
    monkeypatch.setattr(mining_tasks, "_get_operators", AsyncMock(return_value=[]))
    monkeypatch.setattr("backend.alpha_semantic_validator.load_operators_from_db", AsyncMock(return_value=[]))
    retry = Mock(return_value=RuntimeError("scheduled"))
    monkeypatch.setattr(mining_tasks.run_mining_task, "retry", retry)

    with pytest.raises(RuntimeError, match="scheduled"):
        mining_tasks.run_mining_task.run(10, 13)

    assert task.progress_current == 3
    assert run.id == 13 and run.status == "RUNNING"
    assert evolve.await_args.kwargs["target_alphas"] == 5
    assert evolve.await_args.kwargs["max_rounds_per_dispatch"] == 1
    retry.assert_called_once_with(countdown=1, args=(10, 13), max_retries=None)


def test_continuous_task_does_not_finish_at_daily_goal(monkeypatch):
    task = SimpleNamespace(
        id=12, status="RUNNING", region="USA", universe="TOP3000",
        progress_current=4, daily_goal=4, max_iterations=10,
        config={"continuous_mode": True},
    )
    run = SimpleNamespace(
        id=15, task_id=12, status="RUNNING", celery_task_id=None,
        checkpoint={"dataset_id": "news17", "iteration": 10, "total_success": 4},
    )
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[
            SimpleNamespace(scalar_one_or_none=lambda: task),
            SimpleNamespace(rowcount=1),
        ]),
        get=AsyncMock(return_value=run), commit=AsyncMock(),
        refresh=AsyncMock(), rollback=AsyncMock(),
    )

    class SessionFactory:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_):
            return False

    class Brain:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    evolve = AsyncMock(return_value={
        "total_success": 5, "all_alphas": [],
        "yielded_for_dispatch": True, "iterations_completed": 11,
    })
    monkeypatch.setattr(mining_tasks, "AsyncSessionLocal", SessionFactory)
    monkeypatch.setattr(mining_tasks, "MCPBrainAdapter", Brain)
    monkeypatch.setattr(mining_tasks, "MiningAgent", lambda *_: SimpleNamespace(run_evolution_loop=evolve))
    monkeypatch.setattr(mining_tasks, "_get_or_create_run", AsyncMock(return_value=run))
    monkeypatch.setattr(mining_tasks, "_get_datasets_to_mine", AsyncMock(return_value=["news17"]))
    monkeypatch.setattr(mining_tasks, "_get_dataset_fields", AsyncMock(return_value=[{"id": "news_field"}]))
    monkeypatch.setattr(mining_tasks, "_get_operators", AsyncMock(return_value=[]))
    monkeypatch.setattr("backend.alpha_semantic_validator.load_operators_from_db", AsyncMock(return_value=[]))
    retry = Mock(return_value=RuntimeError("scheduled"))
    monkeypatch.setattr(mining_tasks.run_mining_task, "retry", retry)

    with pytest.raises(RuntimeError, match="scheduled"):
        mining_tasks.run_mining_task.run(12, 15)

    assert task.progress_current == 5
    assert run.id == 15 and run.status == "RUNNING"
    assert evolve.await_args.kwargs["continuous_mode"] is True
    retry.assert_called_once_with(countdown=1, args=(12, 15), max_retries=None)


def test_continuous_task_pauses_when_configured_fields_are_unavailable(monkeypatch):
    task = SimpleNamespace(id=12, status="RUNNING", region="USA", universe="TOP3000",
                           progress_current=0, daily_goal=4, max_iterations=10,
                           config={"continuous_mode": True})
    run = SimpleNamespace(id=15, task_id=12, status="RUNNING", celery_task_id=None,
                          checkpoint={}, block_reason=None)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[SimpleNamespace(scalar_one_or_none=lambda: task),
                                       SimpleNamespace(rowcount=1)]),
        get=AsyncMock(return_value=run), commit=AsyncMock(),
        refresh=AsyncMock(), rollback=AsyncMock(),
    )

    class SessionFactory:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_):
            return False

    class Brain:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    monkeypatch.setattr(mining_tasks, "AsyncSessionLocal", SessionFactory)
    monkeypatch.setattr(mining_tasks, "MCPBrainAdapter", Brain)
    monkeypatch.setattr(mining_tasks, "MiningAgent", lambda *_: SimpleNamespace())
    monkeypatch.setattr(mining_tasks, "_get_or_create_run", AsyncMock(return_value=run))
    monkeypatch.setattr(mining_tasks, "_get_datasets_to_mine", AsyncMock(return_value=["news17"]))
    monkeypatch.setattr(mining_tasks, "_get_dataset_fields", AsyncMock(return_value=[]))
    monkeypatch.setattr(mining_tasks, "_get_operators", AsyncMock(return_value=[]))
    monkeypatch.setattr("backend.alpha_semantic_validator.load_operators_from_db", AsyncMock(return_value=[]))

    result = mining_tasks.run_mining_task.run(12, 15)
    assert result == {"skipped": "CONFIG_FIELD_UNAVAILABLE", "run_id": 15}
    assert task.status == run.status == "PAUSED"
    assert run.block_reason == "CONFIG_FIELD_UNAVAILABLE"


def test_continuous_worker_keeps_run_id_after_transaction_rollback(monkeypatch):
    task = SimpleNamespace(id=12, status="RUNNING", region="USA", universe="TOP3000",
                           progress_current=0, daily_goal=4, max_iterations=10,
                           config={"continuous_mode": True})

    class ExpiringRun:
        task_id = 12
        status = "RUNNING"
        celery_task_id = None
        checkpoint = {}
        expired = False

        @property
        def id(self):
            if self.expired:
                raise RuntimeError("expired run attribute loaded after rollback")
            return 15

    run = ExpiringRun()

    async def rollback():
        run.expired = True

    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[SimpleNamespace(scalar_one_or_none=lambda: task),
                                       SimpleNamespace(rowcount=1)]),
        get=AsyncMock(return_value=run), commit=AsyncMock(), refresh=AsyncMock(),
        rollback=rollback,
    )

    class SessionFactory:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_):
            return False

    class Brain:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    monkeypatch.setattr(mining_tasks, "AsyncSessionLocal", SessionFactory)
    monkeypatch.setattr(mining_tasks, "MCPBrainAdapter", Brain)
    monkeypatch.setattr(mining_tasks, "MiningAgent", lambda *_: SimpleNamespace(
        run_evolution_loop=AsyncMock(side_effect=ValueError("transient round failure")),
    ))
    monkeypatch.setattr(mining_tasks, "_get_or_create_run", AsyncMock(return_value=run))
    monkeypatch.setattr(mining_tasks, "_get_datasets_to_mine", AsyncMock(return_value=["news17"]))
    monkeypatch.setattr(mining_tasks, "_get_dataset_fields", AsyncMock(return_value=[{"id": "news_field"}]))
    monkeypatch.setattr(mining_tasks, "_get_operators", AsyncMock(return_value=[]))
    monkeypatch.setattr("backend.alpha_semantic_validator.load_operators_from_db", AsyncMock(return_value=[]))
    retry = Mock(return_value=RuntimeError("scheduled"))
    monkeypatch.setattr(mining_tasks.run_mining_task, "retry", retry)

    with pytest.raises(RuntimeError, match="scheduled"):
        mining_tasks.run_mining_task.run(12, 15)
    retry.assert_called_once_with(countdown=60, args=(12, 15), max_retries=None)
