from types import SimpleNamespace

import pytest

from backend.adapters.brain_adapter import BrainAdapter


@pytest.mark.asyncio
async def test_pre_submit_check_polls_original_alpha_without_post(monkeypatch):
    calls = []
    responses = iter([
        SimpleNamespace(status_code=200, headers={"Retry-After": "0"},
                        json=lambda: {"is": {"checks": []}}),
        SimpleNamespace(status_code=200, headers={}, json=lambda: {"is": {"checks": [
            {"name": "LOW_GLB_AMER_SHARPE", "result": "PASS", "value": 1.06},
            {"name": "POWER_POOL_CORRELATION", "result": "PASS", "value": 0.31}]}}),
    ])
    adapter = object.__new__(BrainAdapter)
    async def get(method, endpoint, **kwargs):
        calls.append((method, endpoint))
        return next(responses)
    monkeypatch.setattr(adapter, "_safe_api_call", get)
    result = await adapter.read_pre_submit_checks("alpha-957", max_wait=2)
    assert result["status"] == "COMPLETE"
    assert len(result["checks"]) == 2
    assert calls == [("GET", "/alphas/alpha-957/check")] * 2


@pytest.mark.asyncio
async def test_pre_submit_check_preserves_pending_when_platform_returns_empty(monkeypatch):
    adapter = object.__new__(BrainAdapter)
    async def get(method, endpoint, **kwargs):
        return SimpleNamespace(status_code=200, headers={}, json=lambda: {"is": {"checks": []}})
    monkeypatch.setattr(adapter, "_safe_api_call", get)
    result = await adapter.read_pre_submit_checks("alpha-957", max_wait=0)
    assert result["status"] == "PENDING"
    assert result["checks"] == []
