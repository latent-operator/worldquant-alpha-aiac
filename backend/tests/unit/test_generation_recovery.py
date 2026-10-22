"""A failed generation attempt must lead to novel exploration, not task completion."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents.evolution_strategy import EvolutionStrategy
from backend.agents.graph.nodes import generation
from backend.agents.graph.nodes.generation import _augment_with_template_candidates
from backend.agents.graph.nodes import evaluation
from backend.agents.graph.state import AlphaCandidate, MiningState
from backend.agents.mining_agent import MiningAgent
from backend.agents import mining_agent
from backend.tasks.mining_tasks import _next_wait_minutes, _schedule_llm_wait


@pytest.mark.asyncio
async def test_static_attempted_expression_is_merged_with_recent_history():
    agent = MiningAgent.__new__(MiningAgent)
    agent._query_attempted_expressions = AsyncMock(return_value=["rank(old)", "rank(recent)"])
    task = SimpleNamespace(id=9, region="USA", universe="TOP3000")

    result = await agent._with_attempted_expression_filter(
        task, "news17", {"attempted_expressions": ["rank(seed)"]}
    )

    assert result["attempted_expressions"] == ["rank(seed)", "rank(old)", "rank(recent)"]
    agent._query_attempted_expressions.assert_awaited_once()


def test_paper_constrained_task_does_not_pad_with_generic_templates(monkeypatch):
    candidate = AlphaCandidate(expression="rank(vec_avg(nws17_d1_event_sentiment_score))")
    monkeypatch.setattr(generation, "_template_candidates", lambda *args, **kwargs: [
        AlphaCandidate(expression="rank(vec_sum(nws17_d1_event_sentiment_score))")
    ])
    result = _augment_with_template_candidates(
        pending_alphas=[candidate],
        fields=[{"id": "nws17_d1_event_sentiment_score", "type": "VECTOR"}],
        operators=[{"name": "rank"}, {"name": "vec_avg"}],
        task_config={
            "paper_reference": "arXiv 2512.19484",
            "research_only_fields": ["nws17_d1_event_sentiment_score"],
        },
        target_count=4,
    )
    assert [item.expression for item in result] == [candidate.expression]


@pytest.mark.asyncio
async def test_no_valid_candidate_records_skip_without_wq(monkeypatch):
    recorded = AsyncMock(return_value={"trace_steps": []})
    monkeypatch.setattr(evaluation, "record_trace", recorded)
    brain = SimpleNamespace(simulate_batch=AsyncMock())
    state = MiningState(
        task_id=9,
        pending_alphas=[AlphaCandidate(expression="invalid", is_valid=False)],
    )

    result = await evaluation.node_simulate(state, brain)

    assert result["simulated_count"] == 0
    assert result["simulation_skip_reason"] == "NO_VALID_CANDIDATES"
    assert recorded.await_args.args[6] == "SKIPPED"
    brain.simulate_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_duplicate_candidates_report_zero_eligible_and_no_wq(monkeypatch):
    class SessionFactory:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_):
            return False

    monkeypatch.setattr("backend.database.AsyncSessionLocal", SessionFactory)
    monkeypatch.setattr(
        "backend.selection_strategy.filter_unsimulated_expressions",
        AsyncMock(return_value=([], ["rank(x)"])),
    )
    recorded = AsyncMock(return_value={"trace_steps": []})
    monkeypatch.setattr(evaluation, "record_trace", recorded)
    brain = SimpleNamespace(simulate_batch=AsyncMock())
    state = MiningState(task_id=9, pending_alphas=[AlphaCandidate(expression="rank(x)", is_valid=True)])

    result = await evaluation.node_simulate(state, brain)

    assert result["eligible_candidate_count"] == 0
    assert result["simulation_skip_reason"] == "NO_NEW_CANDIDATES"
    assert recorded.await_args.args[4]["eligible_candidate_count"] == 0
    brain.simulate_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_dedup_service_failure_does_not_send_wq_or_fake_alpha_failure(monkeypatch):
    class BrokenSession:
        async def __aenter__(self):
            raise OSError("database temporarily unavailable")

        async def __aexit__(self, *_):
            return False

    monkeypatch.setattr("backend.database.AsyncSessionLocal", BrokenSession)
    monkeypatch.setattr(evaluation, "record_trace", AsyncMock(return_value={"trace_steps": []}))
    brain = SimpleNamespace(simulate_batch=AsyncMock())
    state = MiningState(task_id=9, pending_alphas=[AlphaCandidate(expression="rank(x)", is_valid=True)])

    result = await evaluation.node_simulate(state, brain)

    assert result["simulation_skip_reason"] == "DEDUP_UNAVAILABLE"
    assert result["pending_alphas"] == []
    brain.simulate_batch.assert_not_awaited()


def test_paper_recovery_rotates_only_allowed_fields_and_windows():
    config = {
        "target_hypothesis": "Test event sentiment conditioned on relevance and novelty",
        "research_only_fields": [
            "nws17_d1_event_sentiment_score", "nws17_d1_event_relevance",
            "nws17_d1_event_similarity_days", "nws17_d1_rp_story_event_count",
        ],
        "preferred_operators": ["vec_avg", "rank", "ts_mean", "ts_delta"],
    }
    first = mining_agent._paper_recovery_strategy(EvolutionStrategy.default(), config, cursor=0)
    second = mining_agent._paper_recovery_strategy(EvolutionStrategy.default(), config, cursor=1)

    assert first.preferred_fields != second.preferred_fields
    assert set(first.preferred_fields).issubset(set(config["research_only_fields"]))
    assert set(second.preferred_fields).issubset(set(config["research_only_fields"]))
    assert "relevance" in " ".join(first.focus_hypotheses).lower()
    assert any(str(window) in " ".join(second.focus_hypotheses) for window in (3, 5, 10, 20, 40, 60))


def test_three_zero_wq_rounds_escalate_to_wait_without_pause():
    assert [mining_agent._generation_recovery_action(n) for n in (1, 2, 3, 4)] == [
        "REGENERATE", "REGENERATE", "WAITING_LLM", "WAITING_LLM",
    ]


def test_generation_pool_eight_does_not_hide_zero_simulation_candidates():
    assert mining_agent._eligible_candidates_from_trace({
        "eligible_candidate_count": 0,
        "simulated_count": 0,
        "db_duplicates": 8,
        "reason": "NO_NEW_CANDIDATES",
    }) == 0
    assert mining_agent._eligible_candidates_from_trace({
        "eligible_candidate_count": 2,
        "simulated_count": 2,
    }) == 2


@pytest.mark.asyncio
async def test_malformed_codegen_uses_short_paper_aware_retry():
    llm = SimpleNamespace(call=AsyncMock(side_effect=[
        SimpleNamespace(success=False, parsed=None, error="TRUNCATED_JSON"),
        SimpleNamespace(success=True, parsed={"alphas": [
            {"expression": "rank(vec_avg(nws17_d1_event_sentiment_score))"},
        ]}, error=None),
    ]))
    state = MiningState(
        task_id=9, dataset_id="news17",
        fields=[{"id": "nws17_d1_event_sentiment_score", "type": "VECTOR"}],
        operators=[{"name": "rank"}, {"name": "vec_avg"}],
    )
    result = await generation.node_code_gen(state, llm, {"configurable": {"task_config": {
        "paper_reference": "arXiv 2512.19484",
        "target_hypothesis": "Event sentiment conditioned on relevance",
        "research_only_fields": ["nws17_d1_event_sentiment_score"],
        "attempted_expressions": ["rank(old)"] * 500,
    }}})

    assert result["pending_alphas"]
    first, retry = [call.kwargs for call in llm.call.await_args_list]
    assert len(retry["user_prompt"]) < len(first["user_prompt"])
    assert "Event sentiment conditioned on relevance" in retry["user_prompt"]
    assert "nws17_d1_event_sentiment_score" in retry["user_prompt"]
    assert retry["max_tokens"] <= 2048


@pytest.mark.asyncio
async def test_structurally_invalid_codegen_uses_compact_retry():
    llm = SimpleNamespace(call=AsyncMock(side_effect=[
        SimpleNamespace(success=True, parsed={"alphas": "invalid"}, error=None),
        SimpleNamespace(success=True, parsed={"alphas": [
            {"expression": "rank(vec_avg(nws17_d1_event_sentiment_score))"},
        ]}, error=None),
    ]))
    state = MiningState(
        task_id=9, dataset_id="news17",
        fields=[{"id": "nws17_d1_event_sentiment_score", "type": "VECTOR"}],
        operators=[{"name": "rank"}, {"name": "vec_avg"}],
    )
    result = await generation.node_code_gen(state, llm, {"configurable": {"task_config": {
        "paper_reference": "arXiv 2512.19484",
        "target_hypothesis": "Event sentiment conditioned on relevance",
        "research_only_fields": ["nws17_d1_event_sentiment_score"],
    }}})

    assert llm.call.await_count == 2
    assert len(result["pending_alphas"]) == 1


@pytest.mark.asyncio
async def test_third_no_wq_attempt_waits_on_same_run_with_backoff():
    assert [_next_wait_minutes("NO_NEW_CANDIDATES", n) for n in range(5)] == [5, 15, 30, 60, 60]
    checkpoint = {"iteration": 17, "dataset_id": "news17", "no_new_streak": 3, "recovery_cursor": 3}
    task = SimpleNamespace(id=9, status="RUNNING")
    run = SimpleNamespace(
        id=12, status="RUNNING", checkpoint=checkpoint,
        block_reason=None, next_retry_at=None, finished_at=None,
    )
    db = SimpleNamespace(commit=AsyncMock())

    result = await _schedule_llm_wait(db, task, run, "NO_NEW_CANDIDATES", revision=2)

    assert result["_retry"] is True and result["run_id"] == 12
    assert result["retry_countdown"] <= 300
    assert task.status == run.status == "WAITING_LLM"
    assert run.block_reason == "NO_NEW_CANDIDATES"
    assert run.checkpoint["recovery_cursor"] == 3
    assert run.checkpoint["iteration"] == 17
    assert run.next_retry_at is not None
    assert run.finished_at is None
