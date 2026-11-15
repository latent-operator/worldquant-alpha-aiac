import asyncio
import httpx
import pytest

from backend.adapters.brain_adapter import BrainAdapter
from backend.adapters.brain_adapter import match_audited_children


@pytest.mark.asyncio
async def test_single_post_connect_error_is_typed_and_not_retried():
    class Broken(BrainAdapter):
        calls = 0

        async def _safe_api_call(self, method, endpoint, **kwargs):
            self.calls += 1
            assert method == "post" and endpoint == "/simulations"
            assert kwargs["retry_writes"] is False
            raise httpx.ConnectError("connection lost")

    brain = Broken(email="unused", password="unused")
    result = await brain.simulate_alpha("rank(close)", retry_post=False)
    assert brain.calls == 1
    assert result["error_code"] == "TRANSPORT_UNCERTAIN"
    assert result["request_phase"] == "CREATE_POST"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(503, "SERVER_UNCERTAIN"),
                                         (429, "RATE_LIMITED"), (400, "CLIENT_REJECTED")])
async def test_single_post_http_failure_has_stable_error_code(status, code):
    class Rejected(BrainAdapter):
        async def _safe_api_call(self, method, endpoint, **kwargs):
            return _Response({}, status_code=status, headers={}, text="private platform detail")

    result = await Rejected(email="unused", password="unused").simulate_alpha(
        "rank(close)", retry_post=False)
    assert result["error_code"] == code
    assert result["request_phase"] == "CREATE_POST"


class _Response:
    def __init__(self, data, status_code=200, headers=None, text=""):
        self._data = data
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._data


class _TimeoutBoundaryBrainAdapter(BrainAdapter):
    def __init__(self):
        super().__init__(email="unused", password="unused")
        self.calls = []

    async def _safe_api_call(self, method: str, endpoint: str, **kwargs):
        self.calls.append((method, endpoint))
        if endpoint == "https://api.worldquantbrain.com/simulations/parent":
            return _Response({
                "status": "COMPLETE",
                "children": ["child_a", "child_b"],
            })
        if endpoint == "/simulations/child_a":
            return _Response({"status": "COMPLETE", "alpha": "alpha_a"})
        if endpoint == "/simulations/child_b":
            return _Response({"status": "COMPLETE", "alpha": "alpha_b"})
        raise AssertionError(f"unexpected endpoint {endpoint}")

    async def _get_completed_alpha_details(self, alpha_id: str):
        return {
            "success": True,
            "alpha_id": alpha_id,
            "metrics": {"sharpe": 1.0},
        }


class _PendingAlphaDetailsBrainAdapter(BrainAdapter):
    def __init__(self):
        super().__init__(email="unused", password="unused")
        self.calls = 0

    async def _safe_api_call(self, method: str, endpoint: str, **kwargs):
        self.calls += 1
        return _Response({"status": "PENDING"}, headers={"Retry-After": "0.01"})


class _GraceCaptureBrainAdapter(BrainAdapter):
    def __init__(self):
        super().__init__(email="unused", password="unused")
        self.wait_kwargs = None

    async def _safe_api_call(self, method: str, endpoint: str, **kwargs):
        return _Response(
            {"id": "parent"},
            status_code=201,
            headers={"Location": "/simulations/parent"},
        )

    async def _wait_for_multisim(
        self,
        location: str,
        max_wait: int = 1200,
        timeout_grace_seconds: int = 180,
        no_child_timeout_seconds: int = 0,
    ):
        self.wait_kwargs = {
            "location": location,
            "max_wait": max_wait,
            "timeout_grace_seconds": timeout_grace_seconds,
            "no_child_timeout_seconds": no_child_timeout_seconds,
        }
        return {
            "success": True,
            "results": [{"success": True, "alpha_id": "alpha_a"}],
        }


class _NoChildStaleBrainAdapter(BrainAdapter):
    def __init__(self):
        super().__init__(email="unused", password="unused")
        self.calls = 0

    async def _safe_api_call(self, method: str, endpoint: str, **kwargs):
        self.calls += 1
        return _Response(
            {"status": "PENDING", "progress": 0.35, "children": []},
            headers={"Retry-After": "0.01"},
        )


def test_multisim_timeout_boundary_fetches_completed_children():
    adapter = _TimeoutBoundaryBrainAdapter()

    result = asyncio.run(adapter._wait_for_multisim(
        "https://api.worldquantbrain.com/simulations/parent",
        max_wait=0,
    ))

    assert result["success"] is True
    assert [item["alpha_id"] for item in result["results"]] == ["alpha_a", "alpha_b"]
    assert all(item["location"].endswith("/simulations/parent") for item in result["results"])


@pytest.mark.parametrize("size", [2, 4])
def test_audited_multisim_maps_children_by_expression_not_position(size):
    expressions = [f"rank({index})" for index in range(size)]
    results = [{"alpha_id": f"a{index}", "expression": expressions[index]}
               for index in reversed(range(size))]
    matched = match_audited_children(expressions, results)
    assert [item["alpha_id"] for item in matched] == [f"a{index}" for index in range(size)]
    assert match_audited_children(expressions, [{"alpha_id": "missing"}, *results[1:]]) is None


@pytest.mark.parametrize("size", [2, 4])
def test_audited_multi_persists_parent_location_before_polling(size):
    expressions = [f"rank({index})" for index in range(size)]
    class PairAdapter(BrainAdapter):
        async def _safe_api_call(self, method, endpoint, **kwargs):
            assert len(kwargs["json"]) == size
            return _Response({}, status_code=201, headers={"Location": "/simulations/parent"})
        async def _wait_for_multisim(self, location, **kwargs):
            assert events == [("location", "/simulations/parent")]
            return {"success": True, "results": [
                {"alpha_id": f"a{index}", "expression": expressions[index]}
                for index in reversed(range(size))]}
    events = []
    async def saved(location):
        events.append(("location", location))
    results = asyncio.run(PairAdapter().simulate_batch(
        expressions, retry_post=False, audit_match=True, on_location=saved))
    assert [row["alpha_id"] for row in results] == [f"a{index}" for index in range(size)]


@pytest.mark.parametrize("size", [2, 4])
def test_audited_multi_429_is_reported_for_every_child_without_retry(size):
    class Limited(BrainAdapter):
        calls = 0
        async def _safe_api_call(self, method, endpoint, **kwargs):
            self.calls += 1
            return _Response({}, status_code=429, headers={"Retry-After": "30",
                                                           "X-Ratelimit-Remaining": "0"})
    brain = Limited()
    results = asyncio.run(brain.simulate_batch([f"rank({index})" for index in range(size)],
                                               retry_post=False, audit_match=True))
    assert brain.calls == 1
    assert len(results) == size
    assert all(row["http_status"] == 429 and row["retry_after"] == 30 for row in results)


def test_alpha_details_polling_is_bounded():
    adapter = _PendingAlphaDetailsBrainAdapter()

    result = asyncio.run(adapter._get_completed_alpha_details("alpha_pending", max_wait=0.02))

    assert result["success"] is False
    assert result["alpha_id"] == "alpha_pending"
    assert "timed out" in result["error"]
    assert adapter.calls >= 1


def test_simulate_batch_passes_timeout_grace_to_parent_waiter():
    adapter = _GraceCaptureBrainAdapter()

    result = asyncio.run(adapter.simulate_batch(["rank(close)", "rank(open)"], max_wait=12, timeout_grace_seconds=7))

    assert result[0]["alpha_id"] == "alpha_a"
    assert adapter.wait_kwargs == {
        "location": "/simulations/parent",
        "max_wait": 12,
        "timeout_grace_seconds": 7,
        "no_child_timeout_seconds": 0,
    }


def test_multisim_no_child_stale_timeout_returns_fast_trackable_failure():
    adapter = _NoChildStaleBrainAdapter()

    result = asyncio.run(adapter._wait_for_multisim(
        "https://api.worldquantbrain.com/simulations/stuck-parent",
        max_wait=10,
        no_child_timeout_seconds=0.02,
    ))

    assert result["success"] is False
    assert result["location"].endswith("/simulations/stuck-parent")
    assert "no-child timed out" in result["error"]
    assert "children=0" in result["error"]
    assert adapter.calls >= 2
