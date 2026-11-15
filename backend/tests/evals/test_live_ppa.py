"""The live experiment is specified and verified without network calls."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from backend.evals.live_ppa import (
    evidence_key, policy_orders, freeze_experiment, dispatch_gate, analyze_results,
)
from backend.tests.unit.test_ppa_feedback import base as fixture_base
from backend.tests.unit.test_ppa_research_policy import candidate as fixture_candidate


def parent(identifier, *, amer, fitness, sharpe, test_sharpe, lookback):
    row = fixture_candidate(amer=amer, case=f"P{identifier}")
    row.update(id=identifier, expression=f"rank(ts_sum(returns,{lookback}))",
               dataset_id="pv1", mechanism_id="reversal", request_status="RESULT")
    row["metrics"].update(sharpe=sharpe, fitness=fitness, test_sharpe=test_sharpe,
                          turnover=0.1, margin=0.002)
    return row


def feedback():
    rows = [parent(1, amer=1.2, fitness=0.5, sharpe=1.8, test_sharpe=0.2, lookback=2),
            parent(2, amer=1.05, fitness=1.1, sharpe=1.6, test_sharpe=0.3, lookback=3),
            parent(3, amer=0.8, fitness=0.9, sharpe=1.2, test_sharpe=0.4, lookback=4)]
    for row in rows:
        row["metrics"].update(long_count=100, short_count=100)
    return {"base": fixture_base(), "rows": rows, "parent_total_counts": {},
            "exhausted_templates": [], "history_signatures": [],
            "history_behavioral_signatures": [], "strict_max_operator_count": 5,
            "evidence_hash": "historical", "unresolved_requests": 0}


def test_evidence_order_uses_only_frozen_parent_metrics():
    rows = feedback()["rows"]
    assert evidence_key(rows[1]) > evidence_key(rows[0])
    candidates = [{"case_id": f"c{i}", "parent_candidate_id": i} for i in (1, 2, 3)]
    baseline, evidence = policy_orders(candidates, rows)
    assert baseline != evidence
    changed = deepcopy(candidates)
    changed[0]["quality_status"] = "PASS"
    changed[0]["metrics"] = {"sharpe": 99}
    assert policy_orders(changed, rows) == (baseline, evidence)


def test_freeze_is_reproducible_and_reuses_real_ppa_validation():
    data = feedback()
    # Measured lookbacks 2, 3, 4 supply one legal edit per parent.
    first = freeze_experiment(data, source_run_id=28, source_task_id=14,
                              prepare_commit="a" * 40, prepared_at="2026-11-13T00:00:00Z",
                              budget=3, top_k=1, min_exclusive=0)
    second = freeze_experiment(deepcopy(data), source_run_id=28, source_task_id=14,
                               prepare_commit="a" * 40, prepared_at="2026-11-13T00:00:00Z",
                               budget=3, top_k=1, min_exclusive=0)
    assert first == second
    assert len(first["candidates"]) == 3
    assert len({tuple(row["expression_signature"]) for row in first["candidates"]}) == 3
    assert first["manifest_sha256"]
    assert first["source_evidence_hash"] == "historical"
    with pytest.raises(ValueError, match="INSUFFICIENT_CANDIDATES"):
        freeze_experiment(data, source_run_id=28, source_task_id=14,
                          prepare_commit="a" * 40, prepared_at="2026-11-13T00:00:00Z",
                          budget=80, top_k=20, min_exclusive=8)


@pytest.mark.parametrize("change,expected", [
    ({"source_unresolved": 2}, "SOURCE_REQUEST_PENDING"),
    ({"other_active_work": True}, "OTHER_WQ_WORK_ACTIVE"),
    ({"platform_verified": False}, "PLATFORM_UNVERIFIED"),
    ({"now": datetime(2026, 11, 22, tzinfo=timezone.utc)}, "THEME_EXPIRED"),
    ({"lab_database": "alpha_gpt_local"}, "LAB_DATABASE_REQUIRED"),
])
def test_dispatch_gate_stops_before_any_post(change, expected):
    state = {"source_unresolved": 0, "other_active_work": False,
             "platform_verified": True, "theme_ends_at": "2026-11-22T00:00:00Z",
             "now": datetime(2026, 11, 13, tzinfo=timezone.utc),
             "lab_database": "aiac_eval_ppa", "lab_port": 55434,
             "existing_requests": 0, "budget": 80}
    assert dispatch_gate({**state, **change}) == expected
    assert dispatch_gate(state) == "READY"


def test_result_analysis_uses_persisted_status_and_counts_missing_as_failure():
    manifest = {"baseline_order": ["a", "b"], "evidence_order": ["b", "c"],
                "candidates": [{"case_id": name, "parent_candidate_id": i}
                               for i, name in enumerate(("a", "b", "c"), 1)],
                "budget": 3, "top_k": 2, "min_results": 2}
    rows = [{"case_id": "a", "request_status": "RESULT", "quality_status": "OPTIMIZE",
             "ppa_status": "PPA_REJECTED", "effective_improvement": False},
            {"case_id": "b", "request_status": "RESULT", "quality_status": "PASS",
             "ppa_status": "PPA_PENDING", "effective_improvement": True},
            {"case_id": "c", "request_status": "FAILED", "quality_status": None,
             "ppa_status": None, "effective_improvement": False}]
    result = analyze_results(manifest, rows)
    assert result["baseline"]["quality_pass"] == 1
    assert result["evidence"]["quality_pass"] == 1
    assert result["baseline"]["effective_improvement"] == 1
    assert result["evidence"]["effective_improvement"] == 1
    assert result["outcome"] == "NO_DEMONSTRATED_IMPROVEMENT"
    rows[2]["request_status"] = "UNKNOWN"
    assert analyze_results(manifest, rows)["outcome"] == "INCOMPLETE_RECONCILIATION"
    assert analyze_results(manifest, rows)["unknown_count"] == 1


def test_result_analysis_reports_differences_and_parent_uncertainty():
    manifest = {"baseline_order": ["a", "b"], "evidence_order": ["b", "c"],
                "candidates": [{"case_id": "a", "parent_candidate_id": 1},
                               {"case_id": "b", "parent_candidate_id": 2},
                               {"case_id": "c", "parent_candidate_id": 3}],
                "budget": 3, "top_k": 2, "min_results": 3,
                "manifest_sha256": "ab" * 32}
    rows = [{"case_id": cid, "request_status": "RESULT", "quality_status": quality,
             "ppa_status": ppa, "effective_improvement": proxy, "test_sharpe": sharpe}
            for cid, quality, ppa, proxy, sharpe in [
                ("a", "OPTIMIZE", "PPA_REJECTED", False, -0.1),
                ("b", "PASS", "PPA_READY", True, 0.4),
                ("c", "PASS", "PPA_READY", True, 0.6)]]
    result = analyze_results(manifest, rows)
    assert result["outcome"] == "OBSERVED_HARD_OUTCOME_UPLIFT"
    assert result["difference"]["quality_pass"] == 1
    assert result["difference"]["ppa_ready"] == 1
    assert result["difference"]["test_sharpe_mean"] == pytest.approx(0.35)
    assert result["confidence_intervals"]["quality_pass"][0] <= 1
    assert result["confidence_intervals"]["quality_pass"][1] >= 1
    assert result == analyze_results(manifest, rows)
