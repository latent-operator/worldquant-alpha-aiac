import pytest
import httpx
from unittest.mock import AsyncMock
from backend.adapters.brain_adapter import BrainAdapter
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from backend.models import MiningTask, ExperimentRun, ResearchCandidate
from backend.services import research_audit as audit


@pytest.mark.asyncio
async def test_location_is_saved_before_polling():
    brain = BrainAdapter()
    brain._safe_api_call = AsyncMock(return_value=httpx.Response(201, headers={"Location": "/simulations/a"}))
    events = []
    async def saved(location):
        events.append(("saved", location))
    async def poll(location):
        assert events == [("saved", location)]
        return {"alpha_id": "alpha"}
    brain._wait_for_simulation = poll
    result = await brain.simulate_alpha("rank(a)", retry_post=False, on_location=saved)
    assert result["alpha_id"] == "alpha"


@pytest.mark.asyncio
async def test_quota_headers_are_saved_before_polling():
    brain = BrainAdapter()
    brain._safe_api_call = AsyncMock(return_value=httpx.Response(201, headers={
        "Location": "/simulations/a", "X-Ratelimit-Limit": "500",
        "X-Ratelimit-Remaining": "17", "X-Ratelimit-Reset": "3600"}))
    events = []
    async def saved_quota(values):
        events.append(("quota", values))
    async def saved_location(location):
        events.append(("location", location))
    async def poll(location):
        assert events[0] == ("location", location)
        assert events[1] == ("quota", {"limit": 500, "remaining": 17, "reset_seconds": 3600})
        return {"alpha_id": "alpha"}
    brain._wait_for_simulation = poll
    result = await brain.simulate_alpha("rank(a)", retry_post=False,
                                        on_rate_limit=saved_quota, on_location=saved_location)
    assert result["rate_limit"]["remaining"] == 17


@pytest.mark.asyncio
async def test_429_reports_quota_without_retrying_post():
    brain = BrainAdapter()
    brain._safe_api_call = AsyncMock(return_value=httpx.Response(429, headers={
        "Retry-After": "30", "X-Ratelimit-Remaining": "0", "X-Ratelimit-Reset": "3600"}))
    observed = []
    async def save(values):
        observed.append(values)
    result = await brain.simulate_alpha("rank(a)", retry_post=False, on_rate_limit=save)
    assert brain._safe_api_call.await_count == 1
    assert observed == [{"remaining": 0, "reset_seconds": 3600}]
    assert result["http_status"] == 429


async def campaign(async_engine, monkeypatch):
    maker = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(audit, "AsyncSessionLocal", maker)
    async with maker() as db:
        tasks = [MiningTask(task_name=str(i), region="GLB", universe="TOP3000", status="RUNNING") for i in range(2)]
        db.add_all(tasks)
        await db.flush()
        runs = [ExperimentRun(task_id=t.id, status="RUNNING", config_snapshot={"task": {"config": {
            "research_protocol": "theme_ppa_v1", "ppa_manifest": {"theme_id": "nov"}}}}) for t in tasks]
        db.add_all(runs)
        await db.commit()
        return maker, [t.id for t in tasks], [r.id for r in runs]


@pytest.mark.asyncio
async def test_successor_shares_80_budget_and_unknowns(async_engine, monkeypatch):
    maker, tasks, runs = await campaign(async_engine, monkeypatch)
    old = audit.ResearchLedger(tasks[0], runs[0], 80)
    for i in range(79):
        await old.reserve(f"rank(f{i})", {}, "DISCOVERY")
    new = audit.ResearchLedger(tasks[1], runs[1], 80, budget_root_run_id=runs[0])
    cid, reason = await new.reserve("rank(last)", {}, "REFINEMENT")
    assert cid and reason is None
    await new.mark_requested(cid)
    await new.finish(cid, {"error": "timeout"})
    assert (await new.reserve("rank(extra)", {}, "REFINEMENT"))[1] == "BUDGET_EXHAUSTED"
    # Resuming the original run cannot escape the campaign cap either.
    assert (await old.reserve("rank(extra2)", {}, "DISCOVERY"))[1] == "BUDGET_EXHAUSTED"
    assert (await new.reserve("rank(f0)", {}, "REFINEMENT"))[1] == "DUPLICATE"


@pytest.mark.asyncio
async def test_request_location_survives_result_and_recovery(async_engine, monkeypatch):
    maker, tasks, runs = await campaign(async_engine, monkeypatch)
    ledger = audit.ResearchLedger(tasks[0], runs[0], 80)
    cid, _ = await ledger.reserve("rank(a)", {}, "DISCOVERY")
    await ledger.mark_requested(cid)
    await ledger.record_location(cid, "/simulations/abc")
    await ledger.finish(cid, {"success": True, "alpha_id": "one", "metrics": {"sharpe": 1}})
    snapshot = await ledger.recover("rank(a)", {})
    assert snapshot["request"]["location"] == "/simulations/abc"
    assert snapshot["alpha_id"] == "one"
    new = audit.ResearchLedger(tasks[1], runs[1], 80, budget_root_run_id=runs[0])
    await new.reserve("rank(b)", {}, "DISCOVERY")
    assert (await new.recover("rank(a)", {}))["alpha_id"] == "one"


@pytest.mark.asyncio
async def test_pair_location_is_committed_for_both_children(async_engine, monkeypatch):
    maker, tasks, runs = await campaign(async_engine, monkeypatch)
    ledger = audit.ResearchLedger(tasks[0], runs[0], 80)
    first, _ = await ledger.reserve("rank(pair_a)", {}, "DISCOVERY")
    second, _ = await ledger.reserve("rank(pair_b)", {}, "DISCOVERY")
    await ledger.record_locations([first, second], "/simulations/parent", transport="paired_multi")
    async with maker() as db:
        rows = [(await db.get(ResearchCandidate, cid)).result_snapshot for cid in (first, second)]
    assert [row["request"]["location"] for row in rows] == ["/simulations/parent"] * 2
    assert all(row["request"]["transport"] == "paired_multi" for row in rows)


@pytest.mark.asyncio
async def test_quota_snapshot_survives_result_and_recovery(async_engine, monkeypatch):
    maker, tasks, runs = await campaign(async_engine, monkeypatch)
    ledger = audit.ResearchLedger(tasks[0], runs[0], 80)
    cid, _ = await ledger.reserve("rank(a)", {}, "DISCOVERY")
    await ledger.mark_requested(cid)
    await ledger.record_location(cid, "/simulations/a")
    await ledger.record_rate_limit(cid, {"limit": 500, "remaining": 17, "reset_seconds": 3600})
    await ledger.finish(cid, {"success": True, "alpha_id": "alpha"})
    snapshot = await ledger.recover("rank(a)", {})
    assert snapshot["request"]["rate_limit"] == {
        "limit": 500, "remaining": 17, "reset_seconds": 3600}


@pytest.mark.asyncio
async def test_audited_pair_records_parent_location_for_each_case(async_engine, monkeypatch):
    maker, tasks, runs = await campaign(async_engine, monkeypatch)
    class PairBrain:
        supports_request_tracking = True
        async def simulate_audited_pair(self, expressions, **settings):
            await settings["on_location"]("/simulations/parent")
            return [{"success": True, "alpha_id": "a", "expression": expressions[0]},
                    {"success": True, "alpha_id": "b", "expression": expressions[1]}]
    ledger = audit.ResearchLedger(tasks[0], runs[0], 80)
    wrapped = audit.ResearchBudgetBrain(PairBrain(), ledger)
    result = await wrapped.simulate_audited_pair(["rank(a)", "rank(b)"])
    assert [row["alpha_id"] for row in result] == ["a", "b"]
    async with maker() as db:
        rows = (await db.execute(__import__("sqlalchemy").select(ResearchCandidate).where(
            ResearchCandidate.run_id == runs[0]))).scalars().all()
        assert len(rows) == 2
        assert all((row.result_snapshot or {})["request"]["location"] == "/simulations/parent"
                   for row in rows)
        assert all((row.result_snapshot or {})["request"]["transport"] == "paired_multi"
                   for row in rows)


@pytest.mark.asyncio
async def test_reconcile_polls_existing_request_and_never_posts(async_engine, monkeypatch):
    maker, tasks, runs = await campaign(async_engine, monkeypatch)
    ledger = audit.ResearchLedger(tasks[0], runs[0], 80)
    cid, _ = await ledger.reserve("rank(a)", {}, "DISCOVERY")
    await ledger.mark_requested(cid)
    await ledger.record_location(cid, "/simulations/abc")
    calls = []
    async def poll(location):
        calls.append(location)
        return {"success": True, "alpha_id": "one"}
    await ledger.reconcile(poll)
    await ledger.reconcile(poll)
    assert calls == ["/simulations/abc"]
    assert (await ledger.recover("rank(a)", {}))["alpha_id"] == "one"


@pytest.mark.asyncio
async def test_unlocatable_request_remains_charged_unknown(async_engine, monkeypatch):
    maker, tasks, runs = await campaign(async_engine, monkeypatch)
    ledger = audit.ResearchLedger(tasks[0], runs[0], 80)
    cid, _ = await ledger.reserve("rank(a)", {}, "DISCOVERY")
    await ledger.mark_requested(cid)
    async def poll(_):
        pytest.fail("No location must never be polled or resubmitted")
    await ledger.reconcile(poll)
    async with maker() as db:
        assert (await db.get(ResearchCandidate, cid)).request_status == "UNKNOWN"
    assert (await ledger.reserve("rank(a)", {}, "DISCOVERY"))[1] == "DUPLICATE"
