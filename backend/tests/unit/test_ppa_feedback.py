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


def test_seed_selection_skips_exhausted_expression_templates():
    exhausted = candidate(amer=1.2, case="exhausted")
    active = candidate(amer=1.1, case="active")
    active["id"] = 2
    active["expression"] = "rank(ts_sum(returns,2))"
    seeds = feedback.select_active_seeds(
        [exhausted, active], {("reversal", "rank(returns)")}, limit=1)
    assert [row["case_id"] for row in seeds] == ["active"]


def test_seed_selection_uses_distinct_templates_before_window_variants():
    first = candidate(amer=1.2, case="first")
    first["expression"] = "rank(ts_sum(returns,2))"
    second = candidate(amer=1.19, case="second")
    second.update(id=2, expression="rank(ts_sum(returns,3))")
    distinct = candidate(amer=1.1, case="distinct")
    distinct.update(id=3, expression="rank(returns)")
    seeds = feedback.select_active_seeds([first, second, distinct], set(), limit=2)
    assert [row["case_id"] for row in seeds] == ["first", "distinct"]


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


def test_cockpit_empty_stdout_reports_validation_error(tmp_path):
    script = tmp_path / "ask.ps1"
    script.write_text("# mock")
    runner = Mock(return_value=SimpleNamespace(stdout=None))
    with pytest.raises(ValueError, match="COCKPIT_BATCH_INVALID:ValueError:COCKPIT_EMPTY_OUTPUT"):
        feedback.prepare_batch(base(), feedback.build_feedback([candidate()], 51, {"discovery": 4}),
                               script, runner=runner)
    assert runner.call_count == 3
    assert all(call.kwargs.get("errors") == "replace" for call in runner.call_args_list)


def test_cockpit_wrong_phase_repair_names_required_phase(tmp_path):
    script = tmp_path / "ask.ps1"
    script.write_text("# mock")
    prompts = []
    def runner(argv, **kwargs):
        prompts.append(__import__("pathlib").Path(argv[5]).read_text(encoding="utf-8"))
        return SimpleNamespace(stdout=json.dumps({"phase": "discovery", "cases": []}))
    evidence = feedback.build_feedback([candidate()], 51, {"discovery": 4})
    assert evidence["next_phase"] == "refinement"
    with pytest.raises(ValueError, match="PPA_BATCH_PHASE_INVALID"):
        feedback.prepare_batch(base(), evidence, script, runner=runner)
    assert len(prompts) == 3
    assert "phase MUST be refinement" in prompts[0]
    assert "phase MUST be refinement" in prompts[1]


def test_refinement_generation_clamps_to_remaining_slots(tmp_path):
    script = tmp_path / "ask.ps1"
    script.write_text("# mock")
    prompts = []
    proposal = plan()
    proposal["cases"] = []
    for index, window in enumerate((2, 3, 4)):
        item = deepcopy(plan()["cases"][0])
        item["case_id"] = f"r{index}"
        item["expression"] = f"rank(ts_sum(returns,{window}))"
        proposal["cases"].append(item)
    def runner(argv, **kwargs):
        prompts.append(__import__("pathlib").Path(argv[5]).read_text(encoding="utf-8"))
        return SimpleNamespace(stdout=json.dumps(proposal))
    evidence = feedback.build_feedback([candidate()], None,
                                       {"discovery": 4, "refinement": 10}, continuous=True)
    batch = feedback.prepare_batch(base(), evidence, script, runner=runner)
    assert [case["case_id"] for case in batch["cases"]] == ["r0", "r1"]
    assert batch["candidate_generation"]["proposed_cases"] == 3
    assert batch["candidate_generation"]["truncated_cases"] == 1
    assert len(prompts) == 1
    assert "at most 2 cases" in prompts[0]


def test_discovery_top_up_combines_distinct_complete_groups(tmp_path):
    script = tmp_path / "ask.ps1"
    script.write_text("# mock")
    def group(prefix, start):
        return [{"case_id": f"{prefix}{index}", "pair_id": prefix,
                 "arm": "CONTROL" if index == 0 else "TREATMENT",
                 "dataset_id": "pv1", "mechanism_id": "reversal",
                 "expression": f"rank(ts_sum(returns,{start + index}))",
                 "changed_element": "window", "expected_effect": "AMER test",
                 "risk": "noise", "stop_condition": "no gain"} for index in range(4)]
    proposals = iter([{"phase": "discovery", "cases": group("g1", 2)},
                      {"phase": "discovery", "cases": group("g2", 7)}])
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=json.dumps(next(proposals)))
    evidence = feedback.build_feedback([], None, {}, continuous=True, phase_limit=16)
    assert evidence["next_phase"] == "discovery"
    evidence["max_batch_cases"] = 8
    batch = feedback.prepare_batch_filled(base(), evidence, script, runner=runner)
    assert len(batch["cases"]) == 8
    assert len(calls) == 2
    assert batch["candidate_generation"]["top_up_calls"] == 2
    assert {case["pair_id"] for case in batch["cases"]} == {"g1", "g2"}


def test_full_discovery_batch_uses_four_case_cockpit_subcalls(tmp_path):
    script = tmp_path / "ask.ps1"
    script.write_text("# mock")
    proposals = []
    for group_index in range(4):
        prefix = f"g{group_index}"
        proposals.append({"phase": "discovery", "cases": [
            {"case_id": f"{prefix}_{arm_index}", "pair_id": prefix,
             "arm": "CONTROL" if arm_index == 0 else "TREATMENT",
             "dataset_id": "pv1", "mechanism_id": "reversal",
             "expression": f"rank(ts_sum(returns,{2 + group_index * 4 + arm_index}))",
             "changed_element": "window", "expected_effect": "AMER test",
             "risk": "noise", "stop_condition": "no gain"}
            for arm_index in range(4)]})
    prompts = []

    def runner(argv, **kwargs):
        prompts.append(__import__("pathlib").Path(argv[5]).read_text(encoding="utf-8"))
        return SimpleNamespace(stdout=json.dumps(proposals[len(prompts) - 1]))

    evidence = feedback.build_feedback([], None, {}, continuous=True, phase_limit=16)
    batch = feedback.prepare_batch_filled(base(), evidence, script, runner=runner)
    assert len(batch["cases"]) == 16
    assert len(prompts) == 4
    assert all("Generate at most 4 cases" in prompt for prompt in prompts)


def test_refinement_salvages_novel_case_from_exhausted_template(tmp_path):
    script = tmp_path / "ask.ps1"
    script.write_text("# mock")
    evidence = feedback.build_feedback([candidate()], None, {"discovery": 4}, continuous=True)
    evidence["exhausted_templates"] = [["reversal", "rank(ts_sum(returns,#))"]]
    proposal = plan()
    novel = deepcopy(proposal["cases"][0])
    novel["case_id"] = "novel"
    novel["expression"] = "rank(ts_sum(rank(returns),2))"
    proposal["cases"].append(novel)
    runner = Mock(return_value=SimpleNamespace(stdout=json.dumps(proposal)))

    batch = feedback.prepare_batch(base(), evidence, script, runner=runner)
    assert [case["case_id"] for case in batch["cases"]] == ["novel"]
    assert batch["candidate_generation"]["proposed_cases"] == 2
    assert batch["candidate_generation"]["truncated_cases"] == 1


def test_cockpit_failure_does_not_fallback_or_retry(tmp_path):
    script = tmp_path / "ask.ps1"
    script.write_text("# mock")
    runner = Mock(side_effect=TimeoutError("503"))
    with pytest.raises(TimeoutError):
        feedback.prepare_batch(base(), feedback.build_feedback([candidate()], 51, {}), script, runner=runner)
    assert runner.call_count == 1


def test_cockpit_prompt_bounds_large_campaign_history(tmp_path):
    script = tmp_path / "ask.ps1"
    script.write_text("# mock")
    evidence = feedback.build_feedback([candidate()], None, {"discovery": 4}, continuous=True)
    evidence["rows"] = [
        {"id": index, "case_id": f"case_{index}", "expression": f"rank(returns)+{index}",
         "mechanism_id": "reversal", "source": "REFINEMENT",
         "metrics": {"amer_sharpe": 0.9}, "checks": ["x" * 1000]}
        for index in range(100)
    ]
    evidence["history_signatures"] = ["signature" * 12 for _ in range(270)]
    evidence["history_behavioral_signatures"] = ["behavior" * 12 for _ in range(270)]
    captured = []

    def runner(argv, **kwargs):
        captured.append(__import__("pathlib").Path(argv[5]).read_text(encoding="utf-8"))
        raise RuntimeError("captured")

    with pytest.raises(RuntimeError, match="captured"):
        feedback.prepare_batch(base(), evidence, script, runner=runner)
    prompt = captured[0]
    assert len(prompt) < 30_000
    assert "case_99" in prompt and "case_0" not in prompt
    assert '"history_behavioral_signatures": "checked by host"' in prompt
    assert "exhausted_templates" in prompt


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


def test_ranked_fixed_window_mean_rejects_measured_sum_alias():
    data = base()
    data["allowed_operators"] += ["multiply", "ts_mean"]
    evidence = feedback.build_feedback([candidate()], None, {"discovery": 4}, continuous=True)
    settings = data["settings"]
    evidence["history_behavioral_signatures"] = [list(feedback.behavioral_signature(
        "rank(multiply(-1,ts_sum(returns,2)))", settings))]
    proposal = plan()
    proposal["cases"][0]["expression"] = "rank(multiply(-1,ts_mean(returns,2)))"
    with pytest.raises(ValueError, match=r"PPA_DUPLICATE_BEHAVIORAL:rank\(multiply"):
        feedback.freeze_batch(data, evidence, proposal)


def test_generation_context_shows_bounded_history_templates():
    rows = [dict(case_id=f"old_{index}", expression=f"rank(ts_sum(returns,{index}))",
                 settings={}, metrics={}, platform_ppa_evaluation={}) for index in range(100)]
    context = feedback.generation_context({"rows": rows, "seeds": [],
        "history_signatures": [["a", "b"]], "history_behavioral_signatures": [["c", "d"]]})
    assert "rank(ts_sum(returns,#))" in context["avoid_expression_templates"]
    assert context["history_signatures"] == "checked by host"
    assert len(context["avoid_expression_templates"]) <= 64


def test_lookback_mean_sum_alias_is_limited_to_direct_ranked_field():
    settings = base()["settings"]
    assert feedback.behavioral_signature("rank(ts_mean(returns,2))", settings) == (
        feedback.behavioral_signature("rank(ts_sum(returns,2))", settings))
    assert feedback.behavioral_signature("rank(ts_mean(returns,2))", settings) != (
        feedback.behavioral_signature("rank(ts_sum(returns,3))", settings))
    assert feedback.behavioral_signature("rank(multiply(returns,ts_mean(returns,2)))", settings) != (
        feedback.behavioral_signature("rank(multiply(returns,ts_sum(returns,2)))", settings))
    assert feedback.behavioral_signature("rank(ts_mean(close,2))", settings) != (
        feedback.behavioral_signature("rank(ts_sum(close,2))", settings))


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
    assert ("reversal", feedback.expression_template("rank(ts_mean(returns,7))")) in {
        tuple(item) for item in evidence["exhausted_templates"]}


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
    proposal["cases"][0]["expression"] = "rank(ts_std_dev(returns,2))"
    data = base()
    data["allowed_operators"].append("ts_std_dev")
    assert feedback.freeze_batch(data, evidence, proposal)["phase"] == "refinement"


def test_four_nonimproving_windows_exhaust_template_even_with_strong_parent():
    parent = candidate(amer=1.07, sharpe=1.24, fitness=0.46, test=1.52)
    parent.update(id=10, source="DISCOVERY", request_status="RESULT",
                  expression="rank(ts_sum(returns,126))")
    children = []
    for i, window in enumerate((100, 110, 140, 160)):
        row = candidate(amer=1.06, sharpe=1.24, fitness=0.46, test=1.53,
                        case=f"window{i}")
        row.update(id=20 + i, source="REFINEMENT", request_status="RESULT",
                   parent_candidate_id=10,
                   expression=f"rank(ts_sum(returns,{window}))")
        children.append(row)
    evidence = feedback.build_feedback([parent, *children], None,
                                       {"discovery": 4, "refinement": 4}, continuous=True)
    assert ["reversal", "rank(ts_sum(returns,#))"] in evidence["exhausted_templates"]
    assert evidence["seeds"][0]["id"] == 10


def test_recent_failed_windows_exhaust_template_after_older_improvement():
    original = candidate(amer=1.01, sharpe=1.20, fitness=0.42, test=1.50)
    original.update(id=10, source="DISCOVERY", request_status="RESULT",
                    expression="rank(ts_sum(returns,126))")
    improved = candidate(amer=1.07, sharpe=1.24, fitness=0.46, test=1.52)
    improved.update(id=11, source="REFINEMENT", request_status="RESULT",
                    parent_candidate_id=10, expression="rank(ts_sum(returns,125))")
    failures = []
    for i, window in enumerate((100, 110, 140, 160)):
        row = candidate(amer=1.05, sharpe=1.23, fitness=0.46, test=1.52,
                        case=f"later_window{i}")
        row.update(id=20 + i, source="REFINEMENT", request_status="RESULT",
                   parent_candidate_id=11,
                   expression=f"rank(ts_sum(returns,{window}))")
        failures.append(row)
    evidence = feedback.build_feedback([original, improved, *failures], None,
                                       {"discovery": 4, "refinement": 5}, continuous=True)
    assert ["reversal", "rank(ts_sum(returns,#))"] in evidence["exhausted_templates"]

    recovery = candidate(amer=1.14, sharpe=1.26, fitness=0.48, test=1.54)
    recovery.update(id=30, source="REFINEMENT", request_status="RESULT",
                    parent_candidate_id=11, expression="rank(ts_sum(returns,170))")
    recovered = feedback.build_feedback([original, improved, *failures, recovery], None,
                                        {"discovery": 4, "refinement": 6}, continuous=True)
    assert ["reversal", "rank(ts_sum(returns,#))"] not in recovered["exhausted_templates"]
