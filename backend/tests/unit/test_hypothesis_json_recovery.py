"""Structured LLM output must not silently become generic research hypotheses."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents.graph.nodes.generation import node_distill_context, node_hypothesis
from backend.agents.graph.state import MiningState
from backend.agents.services.llm_service import LLMBlockedError, LLMService


def _reply(parsed=None, *, finish_reason="stop", error=None):
    return SimpleNamespace(
        success=parsed is not None,
        parsed=parsed,
        finish_reason=finish_reason,
        error=error,
    )


def _state():
    return MiningState(
        task_id=9,
        dataset_id="news17",
        dataset_category="news",
        fields=[{"id": "news_signal", "type": "MATRIX", "category": "News"}],
    )


@pytest.mark.asyncio
async def test_llm_reports_truncation_and_safe_token_counts():
    service = LLMService(api_key="sk-test-secret", base_url="https://example.invalid/v1", model="MiniMax-M2.5")
    service._ensure_credentials_loaded = AsyncMock()
    service.client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"hypotheses":[{"statement":"cut'),
            finish_reason="length",
        )],
        usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=2048, total_tokens=3048),
    ))

    response = await service.call("system", "user", max_tokens=2048)

    assert not response.success
    assert response.finish_reason == "length"
    assert response.prompt_tokens == 1000
    assert response.completion_tokens == 2048
    assert response.error == "TRUNCATED_JSON"
    assert "sk-test-secret" not in response.error


@pytest.mark.asyncio
async def test_length_finish_is_not_treated_as_complete_even_if_json_parses():
    service = LLMService(api_key="sk-test-secret", base_url="https://example.invalid/v1", model="MiniMax-M2.5")
    service._ensure_credentials_loaded = AsyncMock()
    service.client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"hypotheses":[]}'), finish_reason="length")],
        usage=None,
    ))

    response = await service.call("system", "user")

    assert not response.success
    assert response.error == "TRUNCATED_JSON"


@pytest.mark.asyncio
async def test_empty_length_response_is_classified_as_truncated():
    service = LLMService(api_key="sk-test-secret", base_url="https://example.invalid/v1", model="MiniMax-M2.5")
    service._ensure_credentials_loaded = AsyncMock()
    service.client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=""), finish_reason="length")],
        usage=None,
    ))

    response = await service.call("system", "user")

    assert not response.success
    assert response.finish_reason == "length"
    assert response.error == "TRUNCATED_JSON"


@pytest.mark.asyncio
async def test_hypothesis_retries_with_compact_paper_prompt():
    hypotheses = [
        {"statement": f"Test news mechanism {i}", "rationale": "Economic rationale", "key_fields": ["news_signal"]}
        for i in range(3)
    ]
    llm = SimpleNamespace(call=AsyncMock(side_effect=[
        _reply(finish_reason="length", error="TRUNCATED_JSON"),
        _reply({"hypotheses": hypotheses}),
    ]))

    result = await node_hypothesis(
        _state(), llm,
        {"configurable": {"task_config": {"target_hypothesis": "Paper event recurrence idea"}}},
    )

    assert result["hypotheses"] == hypotheses
    assert llm.call.await_count == 2
    first, second = [call.kwargs for call in llm.call.await_args_list]
    assert first["max_tokens"] == 4096
    assert second["max_tokens"] == 4096
    assert "Paper event recurrence idea" in first["user_prompt"]
    assert "Paper event recurrence idea" in second["user_prompt"]
    assert len(second["user_prompt"]) < len(first["user_prompt"])


@pytest.mark.asyncio
async def test_two_malformed_hypothesis_replies_stop_without_generic_fallback():
    llm = SimpleNamespace(call=AsyncMock(side_effect=[
        _reply(finish_reason="length", error="TRUNCATED_JSON"),
        _reply(finish_reason="stop", error="INVALID_JSON"),
    ]))

    with pytest.raises(LLMBlockedError, match="MALFORMED_HYPOTHESIS"):
        await node_hypothesis(_state(), llm)
    assert llm.call.await_count == 2


@pytest.mark.asyncio
async def test_valid_hypotheses_ignore_malformed_optional_knowledge_section():
    hypotheses = [
        {"statement": f"Test event signal {i}", "rationale": "Event response", "key_fields": ["news_signal"]}
        for i in range(3)
    ]
    llm = SimpleNamespace(call=AsyncMock(return_value=_reply({
        "hypotheses": hypotheses,
        "knowledge_transfer": "not an object",
    })))

    result = await node_hypothesis(_state(), llm)

    assert result["hypotheses"] == hypotheses
    assert result["knowledge_transfer"] == {}
    assert llm.call.await_count == 1


@pytest.mark.asyncio
async def test_distill_retries_only_confirmed_length_truncation():
    llm = SimpleNamespace(call=AsyncMock(side_effect=[
        _reply(finish_reason="length", error="TRUNCATED_JSON"),
        _reply({"selected_concepts": ["News"], "reasoning": "Events"}),
    ]))

    result = await node_distill_context(_state(), llm)

    assert llm.call.await_count == 2
    assert llm.call.await_args_list[0].kwargs["max_tokens"] == 768
    assert llm.call.await_args_list[1].kwargs["max_tokens"] == 1536
    assert "News" in result["distilled_concepts"]


@pytest.mark.asyncio
async def test_distill_does_not_retry_other_json_error():
    llm = SimpleNamespace(call=AsyncMock(return_value=_reply(error="INVALID_JSON")))

    await node_distill_context(_state(), llm)

    assert llm.call.await_count == 1


@pytest.mark.asyncio
async def test_malformed_hypothesis_wait_preserves_checkpoint_and_same_run():
    from backend.tasks.mining_tasks import _schedule_llm_wait, _next_wait_seconds

    assert [_next_wait_seconds("MALFORMED_HYPOTHESIS", n) for n in range(5)] == [30, 120, 300, 900, 1800]
    checkpoint = {"iteration": 17, "dataset_id": "news17"}
    task = SimpleNamespace(id=9, status="RUNNING")
    run = SimpleNamespace(
        id=12, status="RUNNING", checkpoint=checkpoint, block_reason=None,
        error_message=None, next_retry_at=None, finished_at=None,
    )
    db = SimpleNamespace(commit=AsyncMock())

    result = await _schedule_llm_wait(db, task, run, "MALFORMED_HYPOTHESIS", revision=2)

    assert result["_retry"] is True and result["run_id"] == 12
    assert result["retry_countdown"] <= 30
    assert task.status == run.status == "WAITING_LLM"
    assert run.block_reason == "MALFORMED_HYPOTHESIS"
    assert run.checkpoint["iteration"] == checkpoint["iteration"]
    assert run.checkpoint["dataset_id"] == checkpoint["dataset_id"]
    assert run.checkpoint["malformed_hypothesis_attempts"] == 1
    assert run.next_retry_at is not None
    assert 0 < (run.next_retry_at - datetime.now(timezone.utc)).total_seconds() <= 30
    assert run.finished_at is None
    db.commit.assert_awaited_once()

    task.status = run.status = "RUNNING"
    await _schedule_llm_wait(db, task, run, "MALFORMED_HYPOTHESIS", revision=2)
    assert run.checkpoint["malformed_hypothesis_attempts"] == 2

    run.checkpoint = {**run.checkpoint, "iteration": 18}
    await _schedule_llm_wait(db, task, run, "MALFORMED_HYPOTHESIS", revision=2)
    assert run.checkpoint["malformed_hypothesis_attempts"] == 1
