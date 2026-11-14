"""Read-only evidence decision for the durable AIAC research goal."""

from __future__ import annotations

from typing import Any


GOAL_ID = "AIAC-PPA-QUALIFIED-ALPHA"


def _count(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def evaluate_goal(report: dict, *, source: str,
                  claimed_status: str | None = None) -> dict:
    """Decide only what a persisted experiment report can prove."""
    analysis = report.get("analysis")
    if not source:
        raise ValueError("An experiment source is required")
    if analysis is None and isinstance(report.get("candidate_lineage"), list):
        rows = report["candidate_lineage"]
        pending_statuses = {"UNKNOWN", "REQUESTED", "RESERVED"}
        analysis = {
            "result_count": sum(row.get("request_status") == "RESULT" for row in rows),
            "unknown_count": sum(row.get("request_status") == "UNKNOWN" for row in rows),
            "pending_count": sum(row.get("request_status") in pending_statuses for row in rows),
            "evidence": {
                "quality_pass": sum(row.get("request_status") == "RESULT"
                                    and row.get("quality_status") == "PASS" for row in rows),
                "ppa_ready": sum(row.get("request_status") == "RESULT"
                                 and row.get("quality_status") == "PASS"
                                 and row.get("ppa_status") == "PPA_READY" for row in rows),
            },
        }
    if not isinstance(analysis, dict):
        raise ValueError("A saved experiment analysis or candidate lineage is required")
    evidence = analysis.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("Persisted experiment evidence is required")

    results = _count(analysis.get("result_count"), "result_count")
    unknown = _count(analysis.get("unknown_count"), "unknown_count")
    pending = _count(analysis.get("pending_count", unknown), "pending_count")
    quality = _count(evidence.get("quality_pass"), "quality_pass")
    ready = _count(evidence.get("ppa_ready"), "ppa_ready")
    if quality > results or ready > quality:
        raise ValueError("Business evidence counts are inconsistent")

    proven_candidate = None
    for row in report.get("candidates") or report.get("candidate_lineage") or []:
        if (row.get("candidate_id") is not None
                and row.get("wq_alpha_id")
                and row.get("quality_status") == "PASS"
                and row.get("request_status", "RESULT") == "RESULT"
                and (row.get("ppa_evaluation") == "PPA_READY"
                     or row.get("ppa_status") == "PPA_READY")):
            proven_candidate = row
            break

    if unknown or pending or results == 0 or analysis.get("outcome") == "INCOMPLETE_RECONCILIATION":
        status = "EVIDENCE_INSUFFICIENT"
    elif quality == 0:
        status = "NOT_PROVEN"
    elif ready > 0 and proven_candidate is not None:
        status = "PPA_READY_OBSERVED"
    else:
        status = "PASS_OBSERVED"

    return {"goal_id": GOAL_ID, "source": source,
            "run_id": report.get("run_id"), "source_run_id": report.get("source_run_id"),
            "campaign_id": report.get("campaign_id"),
            "campaign_sha256": report.get("campaign_sha256"),
            "manifest_sha256": report.get("manifest_sha256"),
            "status": status,
            "result_count": results, "unknown_requests": unknown,
            "pending_requests": pending, "quality_pass": quality,
            "final_ppa_ready": ready,
            "historical_unknown_retained": _count(report.get("old_unknown_retained", 0),
                                                   "old_unknown_retained"),
            "candidate_id": proven_candidate.get("candidate_id") if proven_candidate else None,
            "wq_alpha_id": proven_candidate.get("wq_alpha_id") if proven_candidate else None,
            "claim_supported": claimed_status is None or claimed_status == status}
