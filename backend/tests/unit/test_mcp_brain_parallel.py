import asyncio
import pytest
from backend.adapters.mcp_brain_adapter import MCPBrainAdapter


@pytest.mark.asyncio
async def test_concurrent_single_requests_enter_fallback_once():
    class Fallback:
        enters = 0
        async def __aenter__(self):
            self.enters += 1
            await asyncio.sleep(0.01)
            return self
        async def __aexit__(self, *args):
            pass
        async def simulate_alpha(self, expression, **kwargs):
            return {"alpha_id": expression}
    fallback = Fallback()
    adapter = MCPBrainAdapter(fallback=fallback)
    async with adapter:
        await asyncio.gather(adapter.simulate_single("a"), adapter.simulate_single("b"))
    assert fallback.enters == 1


@pytest.mark.asyncio
async def test_audited_pair_uses_direct_tracked_adapter():
    class Fallback:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def simulate_batch(self, expressions, **kwargs):
            assert kwargs["retry_post"] is False
            assert kwargs["audit_match"] is True
            return [{"alpha_id": "a", "expression": expressions[0]},
                    {"alpha_id": "b", "expression": expressions[1]}]
    adapter = MCPBrainAdapter(fallback=Fallback())
    async with adapter:
        result = await adapter.simulate_audited_pair(["rank(a)", "rank(b)"])
    assert [item["alpha_id"] for item in result] == ["a", "b"]
