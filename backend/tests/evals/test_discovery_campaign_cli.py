"""Offline checks for frozen campaign evidence and safe operator boundaries."""
from backend.services.ppa_feedback import case_settings, digest
from backend.services.research_audit import signature
from backend.tests.unit.test_ppa_feedback import base as fixture_base
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import scripts.ppa_discovery_campaign as operator
from scripts.ppa_discovery_campaign import (
    _evidence, _history, _new_base,
)


def test_new_base_only_exposes_live_verified_fields():
    selected = {"dataset_id": "news76", "category": "news",
                "field_ids": ["n1", "n2"], "field_metadata": {
                    "n1": {"type": "MATRIX", "account_verified": True,
                           "instrument_coverage": 0.8, "date_coverage": 0.8},
                    "n2": {"type": "MATRIX", "account_verified": True,
                           "instrument_coverage": 0.7, "date_coverage": 0.7}}}
    base = _new_base(fixture_base(), selected, verified_at="2026-11-14T00:00:00Z")
    assert base["datasets"] == {"news76": ["n1", "n2"]}
    assert set(base["mechanisms"]) == {"account_verified_signal"}
    assert all(row["account_verified"] for row in base["field_metadata"]["news76"].values())


def test_old_and_production_signatures_are_carried_into_next_batch():
    base = fixture_base()
    old = {"candidates": [{"expression": "rank(close)"}]}
    previous = [{"batch_sha256": "one", "cases": [{"expression": "rank(open)"}]}]
    source = {"evidence_hash": "source", "history_signatures": [],
              "history_behavioral_signatures": []}
    identities, _ = _history(source, old, previous, base)
    settings = case_settings(base, {})
    assert list(signature("rank(close)", settings)) in identities
    assert list(signature("rank(open)", settings)) in identities
    evidence = _evidence(source, old, previous, base)
    assert evidence["budget_remaining"] is None
    assert evidence["evidence_hash"] == digest({
        "source": "source", "history": evidence["history_signatures"],
        "behavioral": evidence["history_behavioral_signatures"],
        "previous_batches": ["one"]})
    assert _evidence(source, old, previous, base) == evidence


def test_run_waits_24_hours_before_any_account_or_production_read(monkeypatch, tmp_path):
    campaign = {"code_commit": "a" * 40,
                "old_quarantine_until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    monkeypatch.setattr(operator, "_load_campaign", lambda _: (campaign, []))
    monkeypatch.setattr(operator, "_require_clean_commit", lambda: "a" * 40)
    monkeypatch.setattr(operator, "validate_lab_environment", lambda _: True)
    monkeypatch.setattr(operator, "_state", lambda *args: (_ for _ in ()).throw(
        AssertionError("Account or source was read before the wait")))
    result = operator._run(SimpleNamespace(directory=str(tmp_path)))
    assert result["reason"] == "OLD_UNKNOWN_24H_WAIT"
    assert result["sent"] == 0
