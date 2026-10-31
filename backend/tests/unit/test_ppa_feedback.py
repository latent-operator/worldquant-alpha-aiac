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
    assert all(c.args[0][0] == "pwsh" for c in runner.call_args_list)


def test_cockpit_failure_does_not_fallback_or_retry(tmp_path):
    script = tmp_path / "ask.ps1"
    script.write_text("# mock")
    runner = Mock(side_effect=TimeoutError("503"))
    with pytest.raises(TimeoutError):
        feedback.prepare_batch(base(), feedback.build_feedback([candidate()], 51, {}), script, runner=runner)
    assert runner.call_count == 1


def test_frozen_discovery_rejects_unknown_campaign_signature():
    from backend.services.research_audit import signature
    data = base()
    data["datasets"]["pv1"] = ["returns", "adv20", "close", "open"]
    evidence = feedback.build_feedback([], None, {}, continuous=True)
    settings = data["settings"]
    evidence["history_signatures"] = [list(signature("rank(returns)", settings))]
    proposal = {"phase": "discovery", "cases": [dict(case_id=f"c{i}", pair_id="p1",
        arm="CONTROL" if i == 0 else "TREATMENT", dataset_id="pv1",
        mechanism_id="reversal", expression=f"rank({field})", changed_element="field",
        expected_effect="improve AMER", risk="noise", stop_condition="no gain")
        for i, field in enumerate(data["datasets"]["pv1"])]}
    with pytest.raises(ValueError, match="PPA_DUPLICATE_HISTORICAL"):
        feedback.freeze_batch(data, evidence, proposal)


def test_cockpit_repair_receives_exact_validation_code(tmp_path):
    script = tmp_path / "ask.ps1"
    script.write_text("# mock")
    prompts = []
    def runner(argv, **kwargs):
        prompts.append(__import__("pathlib").Path(argv[5]).read_text(encoding="utf-8"))
        return SimpleNamespace(stdout="not-json")
    with pytest.raises(ValueError, match="COCKPIT_BATCH_INVALID:.*JSON"):
        feedback.prepare_batch(base(), feedback.build_feedback([candidate()], 51, {}), script, runner=runner)
    assert len(prompts) == 3
    assert "JSONDecodeError" in prompts[1]


def test_failed_window_template_is_rejected_before_import():
    data = base()
    evidence = feedback.build_feedback([], None, {}, continuous=True)
    evidence["exhausted_templates"] = [["reversal", "rank(ts_sum(returns,#))"]]
    proposal = {"phase": "discovery", "cases": [dict(case_id=f"c{i}", pair_id="p1",
        arm="CONTROL" if i == 0 else "TREATMENT", dataset_id="pv1",
        mechanism_id="reversal", expression=f"rank(ts_sum(returns,{window}))",
        changed_element="window", expected_effect="improve AMER", risk="noise",
        stop_condition="no gain") for i, window in enumerate((3, 5, 10, 20))]}
    with pytest.raises(ValueError, match="PPA_TEMPLATE_EXHAUSTED"):
        feedback.freeze_batch(data, evidence, proposal)


def test_four_failed_windows_exhaust_only_that_template():
    rows = []
    for i, window in enumerate((3, 5, 10, 20)):
        row = candidate(test=-0.1, case=f"case{i}")
        row.update(id=i + 1, expression=f"rank(ts_sum(returns,{window}))",
                   source="DISCOVERY", request_status="RESULT")
        rows.append(row)
    evidence = feedback.build_feedback(rows, None, {}, continuous=True)
    assert ["reversal", "rank(ts_sum(returns,#))"] in evidence["exhausted_templates"]
    assert ["reversal", "rank(ts_mean(returns,#))"] not in evidence["exhausted_templates"]


def test_failed_children_do_not_ban_stronger_parent_or_new_template():
    parent = candidate(amer=0.79, sharpe=1.56, fitness=0.69, test=1.38)
    parent.update(source="DISCOVERY", request_status="RESULT")
    children = []
    for i, window in enumerate((3, 5, 10, 20)):
        row = candidate(amer=0.4, sharpe=0.7, fitness=0.2, test=-0.1, case=f"bad{i}")
        row.update(id=i + 2, source="REFINEMENT", request_status="RESULT",
                   parent_candidate_id=parent["id"], expression=f"rank(ts_sum(returns,{window}))")
        children.append(row)
    evidence = feedback.build_feedback([parent, *children], None,
                                       {"discovery": 4, "refinement": 4}, continuous=True)
    assert "reversal" in evidence["stopped_families"]
    assert evidence["seeds"][0]["id"] == parent["id"]
    proposal = plan()
    proposal["cases"][0]["expression"] = "rank(ts_mean(returns,2))"
    data = base()
    data["allowed_operators"].append("ts_mean")
    assert feedback.freeze_batch(data, evidence, proposal)["phase"] == "refinement"
