import pytest

from backend.services.request_lifecycle import classify_record, classify_result


@pytest.mark.parametrize("snapshot,kind,terminal,pending,retry", [
    ({"alpha_id": "a1"}, "RESULT", True, False, False),
    ({"request_phase": "CREATE_POST", "http_status": 400}, "CLIENT_REJECTED", True, False, False),
    ({"request_phase": "CREATE_POST", "http_status": 429}, "RATE_LIMITED", True, False, True),
    ({"request_phase": "CREATE_POST", "http_status": 408}, "UNKNOWN", False, True, False),
    ({"request_phase": "CREATE_POST", "http_status": 503}, "UNKNOWN", False, True, False),
    ({"request_phase": "CREATE_POST", "http_status": 202}, "UNKNOWN", False, True, False),
    ({"http_status": 400}, "UNKNOWN", False, True, False),
    ({"request_phase": "POLL_ORIGINAL", "http_status": 404, "status": "ERROR"}, "UNKNOWN", False, True, False),
    ({"request_phase": "POLL_ORIGINAL", "http_status": 404}, "UNKNOWN", False, True, False),
    ({"request_phase": "POLL_ORIGINAL", "http_status": 405}, "UNKNOWN", False, True, False),
    ({"request_phase": "POLL_ORIGINAL", "http_status": 429}, "UNKNOWN", False, True, False),
    ({"request_phase": "POLL_ORIGINAL", "http_status": 500}, "UNKNOWN", False, True, False),
    ({"request_phase": "POLL_ORIGINAL", "status": "ERROR"}, "TERMINAL_FAILURE", True, False, False),
    ({"request_phase": "POLL_ORIGINAL", "simulation_status": "FAILED"}, "TERMINAL_FAILURE", True, False, False),
    ({"request_phase": "CREATE_POST", "status": "ERROR"}, "UNKNOWN", False, True, False),
])
def test_request_evidence_requires_provenance(snapshot, kind, terminal, pending, retry):
    evidence = classify_result(snapshot)
    assert (evidence.kind, evidence.terminal, evidence.needs_reconciliation, evidence.retryable_after_wait) == (kind, terminal, pending, retry)


@pytest.mark.parametrize("status", ["RESERVED", "REQUESTED", "UNKNOWN", "FAILED"])
def test_uncommitted_or_contradictory_alpha_requires_repair(status):
    assert classify_record(status, {"alpha_id": "a1"}).needs_reconciliation


def test_persisted_evidence_and_unoccupied_records():
    assert classify_record("RESULT", {}, "a1").kind == "RESULT"
    assert classify_record("RESULT", {}).needs_reconciliation
    assert classify_record("FAILED", {"http_status": 400}).needs_reconciliation
    assert classify_record("NOT_REQUESTED", {}).kind == "NOT_SENT"
    assert not classify_record("NOT_REQUESTED", {}).needs_reconciliation


@pytest.mark.parametrize("status", ["ERROR", "FAILED"])
def test_snapshot_round_trip_preserves_terminal_simulation_status(status):
    from backend.services.research_audit import safe_result
    saved = safe_result({"request_phase": "POLL_ORIGINAL", "status": status})
    assert saved["simulation_status"] == status
    assert saved["status"] is None
    assert classify_result(saved).kind == "TERMINAL_FAILURE"
