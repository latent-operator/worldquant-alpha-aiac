"""Reports preserve recorded states while failing closed on missing evidence."""
from backend.evals.goal_evidence import evaluate_goal
from backend.evals.live_ppa import analyze_results
from backend.evals.snapshot import sanitize_snapshot


def test_unoccupied_frozen_cases_prevent_uplift_with_76_valid_results():
    ids = [f"c{i}" for i in range(80)]
    manifest = {"candidates": [{"case_id": cid, "parent_candidate_id": i}
                               for i, cid in enumerate(ids)],
                "baseline_order": ids, "evidence_order": ids[20:] + ids[:20],
                "budget": 80, "top_k": 20, "min_results": 76}
    rows = [{"case_id": cid, "candidate_id": i + 1,
             "request_status": "RESULT" if i < 76 else "NOT_REQUESTED",
             "wq_alpha_id": f"synthetic-{i}" if i < 76 else None,
             "quality_status": "PASS" if i == 25 else None,
             "ppa_status": "PPA_READY" if i == 25 else None}
            for i, cid in enumerate(ids)]
    analysis = analyze_results(manifest, rows)
    assert analysis["outcome"] == "INCOMPLETE_REQUEST_OCCUPANCY"
    assert analysis["pending_count"] == 0
    assert analysis["unoccupied_count"] == 4
    assert analysis["request_status_counts"]["NOT_REQUESTED"] == 4
    assert sum(analysis["request_status_counts"].values()) == 80
    assert evaluate_goal({"analysis": analysis, "candidate_lineage": rows},
                         source="offline")['status'] == "EVIDENCE_INSUFFICIENT"


def test_definite_failed_with_stale_quality_does_not_count_business_success():
    manifest = {"candidates": [{"case_id": "c1", "parent_candidate_id": 1}],
                "baseline_order": [], "evidence_order": ["c1"],
                "budget": 1, "top_k": 1, "min_results": 0}
    result = analyze_results(manifest, [{"case_id": "c1", "request_status": "FAILED",
        "request_phase": "CREATE_POST", "http_status": 400,
        "quality_status": "PASS", "ppa_status": "PPA_READY", "effective_improvement": True}])
    assert result["evidence"]["quality_pass"] == result["evidence"]["ppa_ready"] == 0
    assert result["evidence"]["effective_improvement"] == 0
    assert result["difference"]["quality_pass"] == 0
    assert result["confidence_intervals"]["quality_pass"] == [0, 0]


def test_failed_without_phase_cannot_complete_an_experiment():
    manifest = {"candidates": [{"case_id": "c1", "parent_candidate_id": 1}],
                "baseline_order": ["c1"], "evidence_order": ["c1"],
                "budget": 1, "top_k": 1, "min_results": 0, "manifest_sha256": "a" * 64}
    result = analyze_results(manifest, [{"case_id": "c1", "request_status": "FAILED",
                                        "http_status": 400}])
    assert result["pending_count"] == 1
    assert result["request_status_counts"]["FAILED"] == 1
    assert result["request_status_counts"]["UNKNOWN"] == 0
    assert result["outcome"] == "INCOMPLETE_RECONCILIATION"


def test_goal_counts_ambiguous_failed_without_rewriting_history():
    report = {"run_id": 1, "candidate_lineage": [
        {"candidate_id": 1, "request_status": "RESULT", "wq_alpha_id": "synthetic-alpha",
         "quality_status": "PASS", "ppa_status": "PPA_READY"},
        {"candidate_id": 2, "request_status": "FAILED", "http_status": 404,
         "request_phase": "POLL_ORIGINAL"}]}
    result = evaluate_goal(report, source="offline-test")
    assert result["pending_requests"] == 1
    assert result["status"] == "EVIDENCE_INSUFFICIENT"


def test_snapshot_retains_terminal_evidence_and_excludes_raw_error():
    result = sanitize_snapshot({"task_id": 1, "run_id": 2, "evidence_pending_count": 0}, [
        {"id": 3, "request_status": "FAILED", "request_phase": "POLL_ORIGINAL",
         "simulation_status": "ERROR", "http_status": 200, "error": "private-expression"}])
    assert result["evidence_pending_count"] == 0
    assert result["candidates"][0]["simulation_status"] == "ERROR"
    assert "private-expression" not in str(result)


def test_old_report_is_readable_but_its_failed_claim_is_downgraded():
    report = {"candidate_lineage": [{"candidate_id": 1, "request_status": "FAILED"}],
              "analysis": {"result_count": 0, "unknown_count": 0, "pending_count": 0,
                           "request_status_counts": {"FAILED": 1},
                           "evidence": {"quality_pass": 0, "ppa_ready": 0}}}
    result = evaluate_goal(report, source="legacy-report")
    assert result["pending_requests"] == 1
    assert result["status"] == "EVIDENCE_INSUFFICIENT"


def test_terminal_snapshot_roundtrip_remains_distinct_from_http_lookup_failure():
    from backend.evals.offline import lifecycle_decision
    from backend.services.research_audit import safe_result
    snapshot = safe_result({"request_phase": "POLL_ORIGINAL", "status": "ERROR",
                            "http_status": 200, "error": "private-expression"})
    public = {"id": 1, "experiment_group": "c1", "request_status": "FAILED",
              "request_phase": snapshot.get("request_phase"),
              "simulation_status": snapshot.get("simulation_status"),
              "http_status": snapshot.get("http_status")}
    sanitized = sanitize_snapshot({"task_id": 1, "run_id": 2}, [public])
    boundary = {"run_id": 2, "latest_run_id": 2, "run_status": "PAUSED",
                "block_reason": "PPA_FIRST_BATCH_REVIEW", "batch_cursor": 1,
                "batch_cases": [{"case_id": "c1"}], "candidates": sanitized["candidates"]}
    assert lifecycle_decision(boundary) == "SAFE_BOUNDARY"
    boundary["candidates"][0]["http_status"] = 404
    assert lifecycle_decision(boundary) == "PPA_HANDOFF_REQUEST_PENDING"
