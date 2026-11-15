"""Pure request evidence rules; result existence is independent of Alpha quality."""
from dataclasses import dataclass, replace
from collections.abc import Mapping


@dataclass(frozen=True)
class RequestEvidence:
    kind: str
    terminal: bool
    needs_reconciliation: bool
    retryable_after_wait: bool


UNKNOWN = RequestEvidence("UNKNOWN", False, True, False)


def classify_result(snapshot) -> RequestEvidence:
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    if snapshot.get("alpha_id"):
        return RequestEvidence("RESULT", True, False, False)
    phase = snapshot.get("request_phase")
    status = snapshot.get("http_status")
    if phase == "CREATE_POST" and type(status) is int and 400 <= status < 500 and status != 408:
        return RequestEvidence("RATE_LIMITED" if status == 429 else "CLIENT_REJECTED", True, False, status == 429)
    simulation_status = snapshot.get("simulation_status") or snapshot.get("status")
    # A failed HTTP lookup cannot prove that the original simulation failed.
    if (phase == "POLL_ORIGINAL" and (status is None or type(status) is int and 200 <= status < 300)
            and str(simulation_status or "").upper() in {"ERROR", "FAILED"}):
        return RequestEvidence("TERMINAL_FAILURE", True, False, False)
    return UNKNOWN


def classify_record(request_status, snapshot, wq_alpha_id=None) -> RequestEvidence:
    snapshot = dict(snapshot) if isinstance(snapshot, Mapping) else {}
    if wq_alpha_id:
        snapshot["alpha_id"] = wq_alpha_id
    evidence = classify_result(snapshot)
    if request_status in {"NOT_REQUESTED", "NOT_SENT"} and not snapshot.get("alpha_id"):
        return RequestEvidence("NOT_SENT", False, False, False)
    # Stored request states must be repaired transactionally before they count as final.
    if request_status in {"RESERVED", "REQUESTED", "UNKNOWN"}:
        return replace(evidence, terminal=False, needs_reconciliation=True, retryable_after_wait=False)
    if request_status in {"RESULT", "CACHED"}:
        return evidence if evidence.kind == "RESULT" else UNKNOWN
    if request_status == "FAILED":
        return replace(evidence, terminal=False, needs_reconciliation=True) if evidence.kind == "RESULT" else evidence
    return UNKNOWN
