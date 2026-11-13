"""Deterministic eval contracts; no model or WQ calls."""
import copy
import json
from collections import Counter
from pathlib import Path

import pytest
import httpx
from deepeval import assert_test
from deepeval.test_case import LLMTestCase

from backend.evals.offline import LifecycleMetric, QualityMetric, ThroughputMetric
from backend.evals.snapshot import sanitize_snapshot, compare_snapshot
from scripts.eval_snapshot import fetch_snapshot


FIXTURE = Path(__file__).with_name("fixtures") / "ppa_runs_v1.json"


def check(metric, payload, expected_success):
    case = LLMTestCase(input="offline AIAC ledger", actual_output=json.dumps(payload))
    metric.measure(case)
    if expected_success:
        assert_test(case, [metric])
    else:
        with pytest.raises(AssertionError):
            assert_test(case, [metric])
    return metric


def boundary(status="RESULT", *, cursor=1, latest_run_id=28, http_status=None):
    return {"run_id": 28, "latest_run_id": latest_run_id,
            "run_status": "PAUSED", "block_reason": "PPA_FIRST_BATCH_REVIEW",
            "batch_cursor": cursor, "batch_cases": [{"case_id": "c1"}],
            "candidates": [{"candidate_id": 1777, "case_id": "c1", "request_status": status,
                            "ppa_evaluation": "PPA_REJECTED" if status == "RESULT" else None,
                            "http_status": http_status}]}


@pytest.mark.parametrize("change", [
    {"candidates.0.request_status": "RESERVED"},
    {"candidates.0.request_status": "REQUESTED"},
    {"candidates.0.request_status": "UNKNOWN"},
    {"batch_cursor": 0},
    {"latest_run_id": 29},
    {"candidates.0.request_status": "FAILED", "candidates.0.http_status": 429},
])
def test_lifecycle_accepts_complete_batch_and_rejects_mutations(change):
    assert check(LifecycleMetric(), boundary(), True).reason == "SAFE_BOUNDARY"
    bad = boundary()
    for path, value in change.items():
        target = bad
        parts = path.split(".")
        for part in parts[:-1]:
            target = target[int(part)] if part.isdigit() else target[part]
        target[parts[-1]] = value
    assert check(LifecycleMetric(), bad, False).reason != "SAFE_BOUNDARY"


def test_quality_uses_persisted_alpha_status_even_with_high_sharpe():
    sample = {"saved_results": 9, "quality_counts": {"OPTIMIZE": 9},
              "reported_quality_pass": 0, "best_sharpe": 1.67}
    assert check(QualityMetric(), sample, True).reason == "PERSISTED_PASS_ZERO"
    changed = {**sample, "reported_quality_pass": 1}
    assert check(QualityMetric(), changed, False).reason == "QUALITY_COUNT_MISMATCH"


def test_throughput_requires_comparable_complete_windows_and_real_overlap():
    before = {"comparison_key": "same-config", "completed_requests": 10,
              "timed_requests": 10, "recovered_requests": 0,
              "wq_wall_method": "INTERVAL_UNION", "wq_wall_seconds": 100, "actual_parent_peak": 2,
              "pause_seconds_by_reason": {"PPA_RATE_LIMIT": 10}, "pending_requests": 0}
    after = {**before, "completed_requests": 20, "timed_requests": 20, "wq_wall_seconds": 100,
             "pause_seconds_by_reason": {"PPA_RATE_LIMIT": 3}}
    assert check(ThroughputMetric(), {"before": before, "after": after,
                                      "claim": "FASTER"}, True).reason == "FASTER_EVIDENCED"
    for changed in ({"before": None}, {"after": {**after, "pending_requests": 2}},
                    {"after": {**after, "actual_parent_peak": None}},
                    {"after": {**after, "timed_requests": 7}},
                    {"after": {**after, "comparison_key": "different"}}):
        payload = {"before": before, "after": after, "claim": "FASTER", **changed}
        assert check(ThroughputMetric(), payload, False).reason == "UNSUPPORTED_SPEED_CLAIM"
        payload["claim"] = "INSUFFICIENT_EVIDENCE"
        assert check(ThroughputMetric(), payload, True).reason == "INSUFFICIENT_EVIDENCE"


def test_historical_snapshots_and_single_field_tampering():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    expected = {18: 212, 25: 16, 26: 20, 27: 28, 28: 9}
    for run in data["runs"]:
        assert run["source_commit"] is None and run["agentsview_session_id"] is None
        assert run["saved_results"] == expected[run["run_id"]]
        assert run["quality_counts"].get("PASS", 0) == 0
        assert check(QualityMetric(), {"saved_results": run["saved_results"],
            "quality_counts": run["quality_counts"], "reported_quality_pass": run["quality_pass"],
            "best_sharpe": run["best_sharpe"]}, True).success
        assert len(run["candidates"]) == run["generated"]
        assert len({c["candidate_id"] for c in run["candidates"]}) == run["generated"]
        assert dict(Counter(c["request_status"] for c in run["candidates"])) == run["request_counts"]
    run28 = next(run for run in data["runs"] if run["run_id"] == 28)
    assert sum(row["request_status"] == "UNKNOWN" for row in run28["candidates"]) == 2
    assert check(LifecycleMetric(), {"run_id": 28, "latest_run_id": 28,
        "run_status": "PAUSED", "block_reason": "PPA_FIRST_BATCH_REVIEW",
        "batch_cursor": 11, "batch_cases": [{"case_id": str(c["candidate_id"])}
            for c in run28["candidates"]],
        "candidates": [{"candidate_id": c["candidate_id"], "case_id": str(c["candidate_id"]),
            "request_status": c["request_status"], "ppa_evaluation": "PPA_REJECTED"}
            for c in run28["candidates"]]}, False).reason == "PPA_HANDOFF_REQUEST_PENDING"
    tampered = copy.deepcopy(run28)
    tampered["quality_pass"] = 1
    assert check(QualityMetric(), {"saved_results": tampered["saved_results"],
        "quality_counts": tampered["quality_counts"], "reported_quality_pass": tampered["quality_pass"]},
        False).reason == "QUALITY_COUNT_MISMATCH"


def test_sanitizer_allows_only_evidence_fields_and_diff_detects_change():
    report = {"run_id": 28, "task_id": 14, "observed_at": "2026-11-13T00:00:00Z",
              "candidate_funnel": {"generated": 1, "saved_wq_results": 1, "quality_pass": 0},
              "quality_counts": {"OPTIMIZE": 1}, "request_counts": {"RESULT": 1},
              "best_sharpe_alpha": {"sharpe": 1.7, "expression": "secret"},
              "ppa_v2_progress": {"actual_parent_peak": 2,
                "timing": {"wq_wall_seconds": 5, "pause_seconds_by_reason": {"RATE": 1}}},
              "expression": "secret"}
    candidates = [{"id": 11, "request_status": "RESULT", "quality_status": "OPTIMIZE",
                   "experiment_group": "c1", "expression": "secret", "credentials": "secret"}]
    snap = sanitize_snapshot(report, candidates)
    serialized = json.dumps(snap)
    assert "secret" not in serialized
    assert snap["candidates"] == [{"candidate_id": 11, "request_status": "RESULT",
                                   "quality_status": "OPTIMIZE", "case_id": "c1"}]
    changed = copy.deepcopy(snap)
    changed["quality_pass"] = 1
    assert compare_snapshot(snap, changed) == {"quality_pass": {"saved": 0, "current": 1}}


def test_read_only_fetch_pages_candidates_and_rejects_remote_host():
    calls = []
    def respond(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("research-report"):
            return httpx.Response(200, json={"task_id": 14, "run_id": 28,
                "candidate_funnel": {"generated": 2, "saved_wq_results": 0,
                                      "quality_pass": 0, "wq_results": 0}})
        offset = int(request.url.params.get("offset", "0"))
        items = [{"id": 1, "experiment_group": "c1", "request_status": "UNKNOWN",
                  "expression": "secret"}, {"id": 2, "experiment_group": "c2",
                  "request_status": "RESULT", "expression": "secret"}][offset:offset+1]
        return httpx.Response(200, json={"items": items, "total": 2, "has_more": offset == 0})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        snap = fetch_snapshot(client, "http://127.0.0.1:8000/api/v1", 28, page_size=1)
        assert [c["candidate_id"] for c in snap["candidates"]] == [1, 2]
        assert "secret" not in json.dumps(snap)
        assert all(method == "GET" for method, _ in calls)
        with pytest.raises(ValueError, match="loopback"):
            fetch_snapshot(client, "https://example.com/api/v1", 28)
