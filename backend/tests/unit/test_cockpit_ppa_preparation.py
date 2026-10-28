"""Host-side Cockpit preparation never calls a live LLM in CI."""

import json
from types import SimpleNamespace

import pytest

from backend.services.theme_ppa import validate_theme_task
from backend.tests.unit.test_theme_ppa import manifest
from scripts.prepare_theme_ppa_with_cockpit import prepare_manifest


def test_cockpit_generates_frozen_plan_with_auditable_source(tmp_path):
    base = manifest()
    plan = {phase: base.pop(phase) for phase in ("discovery", "refinement", "confirmation")}
    base.pop("candidate_generation")
    script = tmp_path / "cockpit-ask.ps1"
    script.write_text("# fake; never executed", encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["check"] is True
        assert "gpt-6-luna" in argv
        prompt_file = argv[argv.index("-PromptFile") + 1]
        prompt = __import__("pathlib").Path(prompt_file).read_text(encoding="utf-8")
        assert base["theme_id"] in prompt
        return SimpleNamespace(stdout=json.dumps(plan), stderr="")

    prepared = prepare_manifest(base, script, runner=fake_run)
    assert len(calls) == 1
    assert prepared["candidate_generation"]["source"] == "cockpit_local_responses"
    assert prepared["candidate_generation"]["model"] == "gpt-6-luna"
    assert validate_theme_task(SimpleNamespace(
        region="USA", universe="TOP3000", dataset_strategy="SPECIFIC",
        target_datasets=["dataset1"],
        config={"research_protocol": "theme_ppa_v1", "research_request_budget": 80,
                "ppa_manifest": prepared},
    )) is None
    prepared["discovery"][0]["expression"] = "rank(other_field)"
    assert validate_theme_task(SimpleNamespace(
        region="USA", universe="TOP3000", dataset_strategy="SPECIFIC",
        target_datasets=["dataset1"],
        config={"research_protocol": "theme_ppa_v1", "research_request_budget": 80,
                "ppa_manifest": prepared},
    )) == "PPA_CANDIDATE_PLAN_CHANGED"


def test_cockpit_rejects_malformed_plan_without_writing_output(tmp_path):
    base = manifest()
    for phase in ("discovery", "refinement", "confirmation"):
        base.pop(phase)
    base.pop("candidate_generation")
    script = tmp_path / "cockpit-ask.ps1"
    script.write_text("# fake", encoding="utf-8")

    def fake_run(_argv, **_kwargs):
        return SimpleNamespace(stdout="not JSON", stderr="")

    with pytest.raises(ValueError, match="COCKPIT_PLAN_INVALID_JSON"):
        prepare_manifest(base, script, runner=fake_run)


def test_cockpit_does_not_call_model_for_non_platform_theme(tmp_path):
    base = manifest()
    base["platform_source_url"] = "https://platform.worldquantbrain.com.evil.example/theme"
    script = tmp_path / "cockpit-ask.ps1"
    script.write_text("# fake", encoding="utf-8")

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("unverified theme must not reach Cockpit")

    with pytest.raises(ValueError, match="PPA_THEME_NOT_PLATFORM_VERIFIED"):
        prepare_manifest(base, script, runner=must_not_run)


def test_cockpit_accepts_account_confirmed_unlimited_simulation_quota(tmp_path):
    base = manifest()
    plan = {phase: base.pop(phase) for phase in ("discovery", "refinement", "confirmation")}
    base.pop("candidate_generation")
    eligibility = base["account_eligibility"]
    eligibility.pop("quota_remaining")
    eligibility.update({"quota_mode": "NO_FIXED_LIMIT", "quota_source": "account_owner"})
    script = tmp_path / "cockpit-ask.ps1"
    script.write_text("# fake", encoding="utf-8")

    def fake_run(_argv, **_kwargs):
        return SimpleNamespace(stdout=json.dumps(plan), stderr="")

    prepared = prepare_manifest(base, script, runner=fake_run)
    assert prepared["account_eligibility"]["quota_mode"] == "NO_FIXED_LIMIT"
