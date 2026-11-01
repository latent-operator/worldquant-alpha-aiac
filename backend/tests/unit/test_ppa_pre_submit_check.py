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


@pytest.mark.asyncio
async def test_pre_submit_check_returns_429_without_internal_retry(monkeypatch):
    adapter = object.__new__(BrainAdapter)
    calls = []
    async def get(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        return SimpleNamespace(status_code=429, headers={"Retry-After": "60"})
    monkeypatch.setattr(adapter, "_safe_api_call", get)
    result = await adapter.read_pre_submit_checks("alpha-957", max_wait=10)
    assert result == {"status": "THROTTLED", "checks": [],
                      "http_status": 429, "retry_after": 60.0}
    assert calls == [("GET", "/alphas/alpha-957/check", {"retry_rate_limit": False})]


@pytest.mark.asyncio
async def test_safe_api_call_can_return_get_429_without_backoff(monkeypatch):
    adapter = object.__new__(BrainAdapter)
    calls = []
    class Client:
        async def get(self, url, **kwargs):
            calls.append(url)
            return SimpleNamespace(status_code=429, headers={"Retry-After": "60"})
    adapter.client = Client()
    result = await adapter._safe_api_call("GET", "/alphas/alpha-957/check",
                                          retry_rate_limit=False)
    assert result.status_code == 429
    assert len(calls) == 1
