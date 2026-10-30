from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from backend.services import ppa_feedback as feedback
from backend.tests.unit.test_ppa_research_policy import candidate


def base():
    return {"theme_id": "nov", "datasets": {"pv1": ["returns"]},
            "allowed_operators": ["rank", "ts_sum"], "max_fields": 3, "max_operator_count": 8,
            "settings": {"decay": 4, "neutralization": "SUBINDUSTRY"},
            "confirmation_settings": {"decay": [2], "neutralization": ["INDUSTRY"]},
            "mechanisms": {"reversal": {"dataset_id": "pv1", "hypothesis": "reversal", "direction": "negative"}}}


def plan():
    return {"phase": "refinement", "cases": [{
        "case_id": "r2", "parent_case_id": "REFINE_2", "expression": "rank(ts_sum(returns,2))",
        "dataset_id": "pv1", "mechanism_id": "reversal", "changed_element": "window",
        "expected_effect": "increase AMER", "risk": "noise", "stop_condition": "no AMER gain",
        "arm": "TREATMENT"}]}


def test_feedback_contains_measurements_and_no_secrets():
    row = candidate()
    row["cookie"] = "secret"
    payload = feedback.build_feedback([row], budget_remaining=51, phase_counts={})
    assert payload["seeds"][0]["metrics"]["amer_sharpe"] == 0.8
    assert "cookie" not in json.dumps(payload)
    changed = candidate(amer=0.3)
    assert feedback.build_feedback([changed], 51, {})["evidence_hash"] != payload["evidence_hash"]


def test_feedback_prefers_ready_parent_over_new_worse_child():
    row = candidate(amer=1.2)
    row["platform_ppa_evaluation"]["status"] = "PPA_READY"
    assert feedback.build_feedback([row, candidate()], 51, {})["next_phase"] == "confirmation"


def test_failed_confirmation_is_not_repeated_forever():
    parent = candidate(amer=1.2)
    parent["platform_ppa_evaluation"]["status"] = "PPA_READY"
    children = [dict(candidate(case=f"conf{i}"), id=i + 2, source="CONFIRMATION",
                     parent_candidate_id=1) for i in range(2)]
    result = feedback.build_feedback([parent, *children], 48,
                                     {"discovery": 4, "confirmation": 2})
    assert not result["finalists"]
    assert result["next_phase"] != "confirmation"


@pytest.mark.parametrize("mutation", ["field", "orphan", "oversize", "phase", "mechanism"])
def test_batch_rejects_invalid_plan(mutation):
    data = plan()
    if mutation == "field": data["cases"][0]["expression"] = "rank(secret_field)"
    if mutation == "orphan": data["cases"][0]["parent_case_id"] = "absent"
    if mutation == "oversize": data["cases"] *= 13
    if mutation == "phase": data["phase"] = "discovery"
    if mutation == "mechanism": data["cases"][0]["mechanism_id"] = "invented"
    evidence = feedback.build_feedback([candidate()], 51, {"discovery": 4})
    with pytest.raises(ValueError):
        feedback.freeze_batch(base(), evidence, data)


def test_frozen_batch_bound_to_feedback_and_budget():
    evidence = feedback.build_feedback([candidate()], 51, {"discovery": 4})
    result = feedback.freeze_batch(base(), evidence, plan())
    assert result["candidate_generation"]["model"] == "gpt-6-luna"
    assert result["evidence_hash"] == evidence["evidence_hash"]
    evidence["budget_remaining"] = 2
    with pytest.raises(ValueError, match="BUDGET"):
        feedback.freeze_batch(base(), evidence, plan())


def test_cockpit_invalid_output_repairs_at_most_twice(tmp_path):
    script = tmp_path / "ask.ps1"
    script.write_text("# mock")
    runner = Mock(return_value=SimpleNamespace(stdout="not-json"))
    with pytest.raises(ValueError):
        feedback.prepare_batch(base(), feedback.build_feedback([candidate()], 51, {}), script, runner=runner)
    assert runner.call_count == 3
    assert all("gpt-6-luna" in c.args[0] for c in runner.call_args_list)


def test_cockpit_failure_does_not_fallback_or_retry(tmp_path):
    script = tmp_path / "ask.ps1"
    script.write_text("# mock")
    runner = Mock(side_effect=TimeoutError("503"))
    with pytest.raises(TimeoutError):
        feedback.prepare_batch(base(), feedback.build_feedback([candidate()], 51, {}), script, runner=runner)
    assert runner.call_count == 1
