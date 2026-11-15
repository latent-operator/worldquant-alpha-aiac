"""Read-only evidence decision for the durable AIAC research goal."""

from __future__ import annotations

from typing import Any
from collections import Counter
import re
from backend.services.request_lifecycle import classify_record


def _pending(rows: list[dict]) -> int:
    return sum(classify_record(row.get("request_status"), row,
                               row.get("wq_alpha_id")).needs_reconciliation for row in rows)


GOAL_ID = "AIAC-PPA-QUALIFIED-ALPHA"
REQUEST_STATES = {"RESULT", "FAILED", "UNKNOWN", "REQUESTED", "RESERVED", "NOT_REQUESTED", "NOT_SENT"}


def _request_counts(counts: dict) -> tuple[int, int, int]:
    if not isinstance(counts, dict) or set(counts) - REQUEST_STATES:
        raise ValueError("Invalid request status counts")
    for name, value in counts.items():
        _count(value, name)
    return (counts.get("RESULT", 0), counts.get("UNKNOWN", 0),
            sum(counts.get(key, 0) for key in ("UNKNOWN", "REQUESTED", "RESERVED")))


def _validate_rows(report: dict, rows: list) -> None:
    ids, alphas = set(), set()
    for row in rows:
        if not isinstance(row, dict) or row.get("request_status") not in REQUEST_STATES:
            raise ValueError("Missing or invalid request status")
        candidate = row.get("candidate_id")
        if type(candidate) is not int or candidate <= 0 or candidate in ids:
            raise ValueError("Missing or duplicate candidate ID")
        ids.add(candidate)
        if "run_id" in row and row["run_id"] != report.get("run_id"):
            raise ValueError("Candidate belongs to a different run")
        if row["request_status"] == "RESULT":
            alpha = row.get("wq_alpha_id")
            if not isinstance(alpha, str) or not alpha.strip() or alpha in alphas:
                raise ValueError("Missing or duplicate WQ Alpha ID")
            alphas.add(alpha)


def _count(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def evaluate_goal(report: dict, *, source: str,
                  claimed_status: str | None = None) -> dict:
    """Decide only what a persisted experiment report can prove."""
    analysis = report.get("analysis")
    for key in ("candidate_lineage", "candidates"):
        if key in report:
            if not isinstance(report[key], list):
                raise ValueError("Candidate lineage must be a list")
            _validate_rows(report, report[key])
    if not source:
        raise ValueError("An experiment source is required")
    if analysis is None and isinstance(report.get("candidate_lineage"), list):
        rows = report["candidate_lineage"]
        analysis = {
            "result_count": sum(row.get("request_status") == "RESULT" for row in rows),
            "unknown_count": sum(row.get("request_status") == "UNKNOWN" for row in rows),
            "pending_count": _pending(rows),
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
    unoccupied = _count(analysis.get("unoccupied_count", 0), "unoccupied_count")
    lineage = report.get("candidate_lineage", report.get("candidates") or [])
    unoccupied = max(unoccupied, sum(classify_record(row.get("request_status"), row,
        row.get("wq_alpha_id")).kind == "NOT_SENT" for row in lineage))
    quality = _count(evidence.get("quality_pass"), "quality_pass")
    ready = _count(evidence.get("ppa_ready"), "ppa_ready")
    if quality > results or ready > quality:
        raise ValueError("Business evidence counts are inconsistent")
    if pending < unknown:
        raise ValueError("Inconsistent pending request count")
    expected_requests = (results, unknown, pending)
    if "request_status_counts" in analysis:
        recorded = _request_counts(analysis["request_status_counts"])
        unoccupied = max(unoccupied, sum(analysis["request_status_counts"].get(key, 0)
                                        for key in ("NOT_REQUESTED", "NOT_SENT")))
        if recorded[:2] != expected_requests[:2] or recorded[2] > pending:
            raise ValueError("Inconsistent request summary")
        if ("candidate_lineage" not in report
                and analysis["request_status_counts"].get("FAILED", 0)):
            # A count alone cannot establish whether FAILED has terminal evidence.
            pending = max(pending, recorded[2] + analysis["request_status_counts"]["FAILED"])
    if "candidate_lineage" in report:
        actual = Counter(row["request_status"] for row in report["candidate_lineage"])
        derived_pending = _pending(report["candidate_lineage"])
        # Old exports counted only UNKNOWN/REQUESTED/RESERVED. Reassess their
        # evidence without rewriting the file or accepting its completion claim.
        if pending == _request_counts(actual)[2]:
            pending = max(pending, derived_pending)
        if (_request_counts(actual)[:2] != expected_requests[:2]
                or derived_pending != pending
                or ("request_status_counts" in analysis
                    and actual != Counter(analysis["request_status_counts"]))):
            raise ValueError("Inconsistent request lineage and summary")
        passed = [row for row in report["candidate_lineage"]
                  if row["request_status"] == "RESULT" and row.get("quality_status") == "PASS"]
        if quality > len(passed) or ready > sum(row.get("ppa_status") == "PPA_READY" for row in passed):
            raise ValueError("Business summary exceeds complete candidate evidence")

    proven_candidate = None
    for row in report.get("candidate_lineage", report.get("candidates") or []):
        if (row.get("candidate_id") is not None
                and row.get("wq_alpha_id")
                and row.get("quality_status") == "PASS"
                and row.get("request_status") == "RESULT"
                and (row.get("ppa_evaluation") == "PPA_READY"
                     or row.get("ppa_status") == "PPA_READY")):
            proven_candidate = row
            break

    if (unknown or pending or unoccupied or results == 0
            or analysis.get("outcome") in {"INCOMPLETE_RECONCILIATION", "INCOMPLETE_REQUEST_OCCUPANCY"}):
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
            "unoccupied_requests": unoccupied,
            "final_ppa_ready": ready,
            "historical_unknown_retained": _count(report.get("old_unknown_retained", 0),
                                                   "old_unknown_retained"),
            "candidate_id": proven_candidate.get("candidate_id") if proven_candidate else None,
            "wq_alpha_id": proven_candidate.get("wq_alpha_id") if proven_candidate else None,
            "claim_supported": claimed_status is None or claimed_status == status}


def require_goal_ready(report: dict, *, source: str) -> dict:
    """Fail closed for goal acceptance; report-only success is not acceptance."""
    evaluate_goal(report, source=source)
    # A comparison's top-20 summary is not the complete research goal population.
    complete = {key: value for key, value in report.items() if key not in {"analysis", "candidates"}}
    result = evaluate_goal(complete, source=source)
    digest = result.get("campaign_sha256") or result.get("manifest_sha256")
    if (result["status"] != "PPA_READY_OBSERVED"
            or type(result["run_id"]) is not int or result["run_id"] <= 0
            or not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest)
            or not isinstance(report.get("candidate_lineage"), list)):
        raise ValueError("Goal acceptance requires reconciled lineage, run, manifest and final PPA evidence")
    return result
