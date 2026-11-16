"""Offline regressions for the actual discovery dispatch and CLI boundary."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util

import pytest
import httpx
from types import SimpleNamespace

from backend.evals.discovery_campaign import campaign_gate, freeze_campaign_batch
from backend.tests.evals.test_discovery_campaign import campaign_and_batch


def test_missing_source_identity_is_not_clearance():
    campaign, _ = campaign_and_batch()
    assert campaign_gate({"source_unresolved": 0, "other_active_work": False,
                          "platform_verified": True}, campaign["base"]) == "SOURCE_RUN_CHANGED"


def isolated_source_state():
    from backend.services.ppa_feedback import digest
    record = {"task_id": 14, "run_id": 28, "manifest_sha256": "a"*64,
              "blocked_signatures": [{"expression_hash": "b"*64, "settings_hash": "c"*64}]}
    exclusions = {"version": 1, "manifests": [record]}
    exclusions["exclusions_sha256"] = digest(exclusions)
    return {"source_unresolved": 2, "source_run_current": True,
        "source_task_id": 14, "source_run_id": 28, "source_task_status": "STOPPED",
        "source_block_reason": "PPA_HANDOFF_ISOLATED", "other_active_work": False,
        "platform_verified": True, "isolation_exclusions": exclusions,
        "quarantine_inventory": {"status": "READY", "account_authenticated": True,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "old_unknown_retained": 45, "old_unknown_still_unreconciled": True,
            "matched_old_alpha_ids": [], "activity_visibility": "UNAVAILABLE",
            "active_endpoint_status": 405, "active_simulation_count": None}}


def test_audited_source_isolation_allows_new_experiment_without_clearing_unknown():
    campaign, _ = campaign_and_batch()
    state = isolated_source_state()
    assert campaign_gate(state, campaign['base']) == 'READY'
    assert state['source_unresolved'] == 2


@pytest.mark.parametrize('change', [
    {'source_block_reason': None}, {'source_task_status': 'PAUSED'},
    {'source_run_id': 29}, {'quarantine_inventory': {}}, {'isolation_exclusions': {}},
    {'other_active_work': True}, {'platform_verified': False},
])
def test_isolation_is_not_an_unconditional_pending_bypass(change):
    campaign, _ = campaign_and_batch()
    assert campaign_gate({**isolated_source_state(), **change}, campaign['base']) != 'READY'


@pytest.mark.parametrize('change', [
    {'checked_at': '2026-01-01T00:00:00Z'}, {'matched_old_alpha_ids': ['old-alpha']},
    {'account_authenticated': False}, {'status': 'BLOCKED'},
    {'activity_visibility': 'PARTIAL'}, {'active_endpoint_status': 500},
])
def test_isolated_source_still_requires_fresh_account_inventory(change):
    campaign, _ = campaign_and_batch()
    state = isolated_source_state()
    state['quarantine_inventory'].update(change)
    assert campaign_gate(state, campaign['base']) == 'SOURCE_REQUEST_PENDING'


def test_pilot_cannot_freeze_a_second_batch():
    campaign, batch = campaign_and_batch()
    campaign["execution_mode"] = "PILOT_FOUR"
    with pytest.raises(ValueError, match="PILOT_BATCH_INVALID"):
        freeze_campaign_batch(campaign, batch["evidence"],
                              {"phase": "discovery", "cases": batch["cases"]}, index=2)


def test_global_history_service_is_present():
    assert importlib.util.find_spec("backend.services.research_history") is not None


class InventoryBrain:
    BASE_URL = "https://example.invalid"
    def __init__(self, *, partial=False):
        self.partial = partial
        self.calls = []
        self.client = self

    async def get(self, url, *, params=None, **kwargs):
        return await self._safe_api_call("GET", url.removeprefix(self.BASE_URL), params=params)

    async def _safe_api_call(self, method, path, *, params=None):
        assert method == "GET"
        self.calls.append((path, params))
        if path == "/users/self":
            return httpx.Response(200, json={})
        if path == "/simulations":
            return httpx.Response(405)
        if self.partial:
            return httpx.Response(400)
        rows = [] if params["hidden"] else [{"id": "unrelated", "dateCreated": "2026-11-10T00:00:00Z",
                                              "regular": {"code": "rank(other)"}}]
        return httpx.Response(200, json={"count": len(rows), "results": rows})


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_inventory_keeps_unavailable_and_partial_evidence(monkeypatch, partial):
    import scripts.ppa_discovery_campaign as cli
    async def old(*args):
        return {"run_id": 1, "unknown_count": 45, "unknown_expressions": ["rank(old)"],
                "first_request_at": "2026-11-12T00:00:00Z"}
    monkeypatch.setattr(cli, "_old_ledger", old)
    brain = InventoryBrain(partial=partial)
    proof = await cli._old_inventory(brain, None, {"old_lab_run_id": 1, "old_manifest_sha256": "old"})
    if partial:
        assert proof["status"] == "BLOCKED"
        assert proof["reason"] == "WQ_ALPHA_INVENTORY_PARTIAL"
        assert len(brain.calls) == 2  # No retries or further endpoints after HTTP 400.
    else:
        assert proof["status"] == "READY"
        assert proof["activity_visibility"] == "UNAVAILABLE"
        assert proof["active_simulation_count"] is None
        assert {branch["hidden"] for branch in proof["alpha_windows"]} == {False, True}


@pytest.mark.asyncio
async def test_missing_temp_manifest_can_use_explicit_read_only_ledger_reference(async_engine):
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import sessionmaker
    from backend.evals.live_runner import _open_lab_run
    from backend.tests.evals.test_live_runner import manifest
    from backend.models import ResearchCandidate
    import scripts.ppa_discovery_campaign as cli
    maker = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    task_id, run_id = await _open_lab_run(maker, manifest())
    async with maker() as db:
        for index in range(80):
            db.add(ResearchCandidate(task_id=task_id, run_id=run_id, source='EVAL_BACKTEST',
                family_id='old', expression=f'rank(field{index})', expression_hash=str(index),
                settings_hash='settings', settings={}, experiment_group=f'case{index}',
                request_status='RESULT' if index<35 else 'UNKNOWN',
                result_snapshot={'request': {'finished_at':'2026-11-14T00:00:00Z'}}))
        await db.commit()
    assert hasattr(cli, '_old_ledger_reference'), 'ledger recovery entry is missing'
    reference = await cli._old_ledger_reference(maker, run_id)
    assert reference['reference_kind'] == 'LEDGER_SNAPSHOT_NOT_ORIGINAL_MANIFEST'
    assert reference['manifest_sha256'] == manifest()['manifest_sha256']
    assert reference['budget'] == len(reference['candidates']) == 80
    assert reference['old_unknown_count'] == 45
    assert reference['ledger_snapshot_sha256']
    assert (await cli._old_ledger(maker, reference))['unknown_count'] == 45
