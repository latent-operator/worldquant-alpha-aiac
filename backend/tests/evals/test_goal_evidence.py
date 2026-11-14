"""The durable research goal must follow persisted business evidence."""

import copy
import pytest
from backend.evals.goal_evidence import evaluate_goal


def test_pending_requests_are_insufficient_even_with_results():
    report = {"analysis": {"outcome": "INCOMPLETE_RECONCILIATION",
                           "result_count": 35, "unknown_count": 45,
                           "baseline": {"quality_pass": 0, "ppa_ready": 0},
                           "evidence": {"quality_pass": 0, "ppa_ready": 0}}}
    result = evaluate_goal(report, source="lab-run-1")
    assert result["status"] == "EVIDENCE_INSUFFICIENT"
    assert result["unknown_requests"] == 45
    assert result["quality_pass"] == 0


def test_zero_pass_cannot_be_upgraded_by_proxy_claim():
    report = {"analysis": {"outcome": "COMPLETE", "result_count": 80,
                           "unknown_count": 0,
                           "evidence": {"quality_pass": 0, "ppa_ready": 0,
                                        "test_sharpe_mean": 1.8}}}
    result = evaluate_goal(report, source="lab-run-2", claimed_status="PASS_OBSERVED")
    assert result["status"] == "NOT_PROVEN"
    assert result["claim_supported"] is False


def test_persisted_pass_and_final_ppa_are_distinct_levels():
    report = {"analysis": {"outcome": "COMPLETE", "result_count": 16,
                           "unknown_count": 0,
                           "evidence": {"quality_pass": 1, "ppa_ready": 0}}}
    assert evaluate_goal(report, source="lab-run-3")["status"] == "PASS_OBSERVED"
    report["analysis"]["evidence"]["ppa_ready"] = 1
    assert evaluate_goal(report, source="lab-run-3")["status"] == "PASS_OBSERVED"
    report["candidates"] = [{"candidate_id": 71, "wq_alpha_id": "alpha-71",
                              "request_status": "RESULT", "quality_status": "PASS", "ppa_evaluation": "PPA_READY"}]
    result = evaluate_goal(report, source="lab-run-3")
    assert result["status"] == "PPA_READY_OBSERVED"
    assert result["wq_alpha_id"] == "alpha-71"


def test_discovery_report_uses_final_ppa_not_preliminary_platform_status():
    report = {"campaign_id": "new-fields", "campaign_sha256": "manifest-hash",
              "run_id": 2, "old_unknown_retained": 45,
              "candidate_lineage": [{"candidate_id": 81, "request_status": "RESULT",
                                     "wq_alpha_id": "alpha-81", "quality_status": "PASS",
                                     "platform_ppa_status": "PPA_READY", "ppa_status": None}]}
    result = evaluate_goal(report, source="campaign-2")
    assert result["status"] == "PASS_OBSERVED"
    assert result["historical_unknown_retained"] == 45
    assert result["campaign_sha256"] == "manifest-hash"
    assert result["unknown_requests"] == 0
    report["candidate_lineage"][0]["ppa_status"] = "PPA_READY"
    result = evaluate_goal(report, source="campaign-2")
    assert result["status"] == "PPA_READY_OBSERVED"
    assert result["candidate_id"] == 81
    report["candidate_lineage"].append({"candidate_id": 82, "request_status": "UNKNOWN",
                                         "quality_status": None, "ppa_status": None})
    assert evaluate_goal(report, source="campaign-2")["status"] == "EVIDENCE_INSUFFICIENT"


def test_rejects_contradictory_request_summary():
    report = {"analysis": {"result_count": 1, "unknown_count": 0, "pending_count": 0,
                           "request_status_counts": {"RESULT": 1, "UNKNOWN": 45},
                           "evidence": {"quality_pass": 1, "ppa_ready": 1}}}
    with pytest.raises(ValueError, match="request"):
        evaluate_goal(report, source="test")


@pytest.mark.parametrize("mutation", ["missing_status", "unknown_status", "duplicate", "wrong_run", "missing_alpha"])
def test_rejects_incomplete_or_ambiguous_lineage(mutation):
    row = {"candidate_id": 1, "run_id": 2, "request_status": "RESULT",
           "wq_alpha_id": "a", "quality_status": "PASS", "ppa_status": "PPA_READY"}
    rows = [row]
    if mutation == "missing_status": del row["request_status"]
    if mutation == "unknown_status": row["request_status"] = "SUCCESS"
    if mutation == "duplicate": rows.append(copy.deepcopy(row))
    if mutation == "wrong_run": row["run_id"] = 3
    if mutation == "missing_alpha": del row["wq_alpha_id"]
    with pytest.raises(ValueError):
        evaluate_goal({"run_id": 2, "candidate_lineage": rows}, source="test")


def test_summary_cannot_hide_pending_lineage():
    report = {"analysis": {"result_count": 1, "unknown_count": 0, "pending_count": 0,
                            "evidence": {"quality_pass": 0, "ppa_ready": 0}},
              "candidate_lineage": [{"candidate_id": 1, "request_status": "UNKNOWN"}]}
    with pytest.raises(ValueError, match="request"):
        evaluate_goal(report, source="test")


def test_closure_requires_ready_and_provenance():
    from backend.evals.goal_evidence import require_goal_ready
    with pytest.raises(ValueError):
        require_goal_ready({"analysis": {"result_count": 1, "unknown_count": 0,
                           "evidence": {"quality_pass": 1, "ppa_ready": 1}}}, source="test")
    report = {"run_id": 2, "campaign_sha256": "a" * 64,
              "candidate_lineage": [{"candidate_id": 1, "request_status": "RESULT",
                 "wq_alpha_id": "a", "quality_status": "PASS", "ppa_status": "PPA_READY"}]}
    assert require_goal_ready(report, source="test")["candidate_id"] == 1
    del report["campaign_sha256"]
    with pytest.raises(ValueError): require_goal_ready(report, source="test")


def test_closure_does_not_use_unrelated_partial_candidate_list():
    from backend.evals.goal_evidence import require_goal_ready
    report = {"run_id": 2, "manifest_sha256": "a" * 64,
        "analysis": {"result_count": 1, "unknown_count": 0, "pending_count": 0,
                     "evidence": {"quality_pass": 1, "ppa_ready": 1}},
        "candidate_lineage": [{"candidate_id": 1, "request_status": "RESULT",
           "wq_alpha_id": "actual", "quality_status": "FAIL", "ppa_status": "PPA_REJECTED"}],
        "candidates": [{"candidate_id": 99, "request_status": "RESULT",
           "wq_alpha_id": "unrelated", "quality_status": "PASS", "ppa_status": "PPA_READY"}]}
    with pytest.raises(ValueError): require_goal_ready(report, source="test")
