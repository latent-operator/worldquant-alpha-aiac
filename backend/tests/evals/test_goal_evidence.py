"""The durable research goal must follow persisted business evidence."""

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
                              "quality_status": "PASS", "ppa_evaluation": "PPA_READY"}]
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
