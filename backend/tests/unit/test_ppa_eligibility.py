from backend.services.ppa_eligibility import evaluate_ppa_candidate
from backend.services.research_audit import safe_result
from backend.services.research_report import candidate_public
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.adapters.brain_adapter import BrainAdapter
from backend.adapters.mcp_brain_adapter import MCPBrainAdapter


MANIFEST = {"theme_id": "theme-2026-11", "ppa_corr_max": 0.5,
            "turnover_min": 0.01, "turnover_max": 0.7, "min_positions": 10,
            "theme_requirements": {"amer_sharpe_strictly_greater_than": 1.0},
            "eligible_classification_ids": ["POWER_POOL_ELIGIBLE"],
            "required_pre_submit_checks": ["MATCHES_THEMES"]}


def ready_snapshot():
    return {
        "stage": "IS", "status": "UNSUBMITTED", "can_submit": True,
        "platform_can_submit": True,
        "classifications": [{"name": "POWER_POOL", "value": "POWER_POOL_ELIGIBLE"}],
        "themes": [{"id": "theme-2026-11"}],
        "checks": [{"name": "MATCHES_THEMES", "result": "PASS"}],
        "metrics": {"sharpe": 1.3, "amer_sharpe": 1.2, "two_year_sharpe": 0.8,
                    "test_sharpe": 0.6, "turnover": 0.2,
                    "long_count": 100, "short_count": 100},
    }


def test_ready_requires_explicit_platform_and_robustness_evidence():
    result = evaluate_ppa_candidate(ready_snapshot(), MANIFEST, power_pool_corr=0.3)
    assert result["status"] == "PPA_READY"
    assert result["reasons"] == []
    assert result["theme_id"] == "theme-2026-11"


def test_missing_amer_is_pending_even_when_every_other_check_passes():
    snapshot = ready_snapshot()
    snapshot["metrics"].pop("amer_sharpe")
    result = evaluate_ppa_candidate(snapshot, MANIFEST, 0.3)
    assert result["status"] == "PPA_PENDING"
    assert "AMER_SHARPE_MISSING" in result["reasons"]


def test_conflicting_amer_evidence_is_not_ready():
    snapshot = ready_snapshot()
    snapshot["checks"].append({"name": "LOW_GLB_AMER_SHARPE", "value": 0.8})
    result = evaluate_ppa_candidate(snapshot, MANIFEST, 0.3)
    assert result["status"] != "PPA_READY"
    assert "AMER_EVIDENCE_CONFLICT" in result["reasons"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 1.0, 0.9])
def test_amer_boundary_and_nonfinite_never_ready(value):
    snapshot = ready_snapshot()
    snapshot["metrics"]["amer_sharpe"] = value
    assert evaluate_ppa_candidate(snapshot, MANIFEST, 0.3)["status"] != "PPA_READY"


def test_platform_power_pool_classification_can_confirm_empty_manifest_ids():
    provisional = {**MANIFEST, "eligible_classification_ids": []}
    result = evaluate_ppa_candidate(ready_snapshot(), provisional, 0.3)
    assert result["status"] == "PPA_READY"
    assert result["reasons"] == []


def test_empty_manifest_ids_need_explicit_positive_platform_classification():
    provisional = {**MANIFEST, "eligible_classification_ids": []}
    snapshot = ready_snapshot()
    snapshot["classifications"] = [{"name": "POWER_POOL", "value": "INELIGIBLE"}]
    assert evaluate_ppa_candidate(snapshot, provisional, 0.3)["status"] == "PPA_REJECTED"
    snapshot["classifications"] = [{"name": "POWER_POOL", "value": "UNKNOWN"}]
    assert evaluate_ppa_candidate(snapshot, provisional, 0.3)["status"] == "PPA_PENDING"
    snapshot["classifications"] = []
    assert evaluate_ppa_candidate(snapshot, provisional, 0.3)["status"] == "PPA_PENDING"


def test_empty_manifest_ids_require_platform_presubmit_confirmation():
    provisional = {**MANIFEST, "eligible_classification_ids": []}
    snapshot = ready_snapshot()
    snapshot["platform_can_submit"] = False
    assert evaluate_ppa_candidate(snapshot, provisional, 0.3)["status"] == "PPA_PENDING"


def test_official_name_only_manifest_cannot_produce_ready():
    provisional = {**MANIFEST, "theme_id": None,
                   "theme_name": "GLB/D1 Power Pool Nov'26",
                   "eligible_classification_ids": []}
    result = evaluate_ppa_candidate(ready_snapshot(), provisional, 0.3)
    assert result["status"] == "PPA_PENDING"
    assert "THEME_ID_MISSING" in result["reasons"]


def test_wrong_theme_is_rejected():
    snapshot = ready_snapshot()
    snapshot["themes"] = [{"id": "other-theme"}]
    assert evaluate_ppa_candidate(snapshot, MANIFEST, 0.3)["status"] == "PPA_REJECTED"


def test_pending_check_cannot_be_ready():
    snapshot = ready_snapshot()
    snapshot["checks"][0]["result"] = "PENDING"
    snapshot["can_submit"] = False
    assert evaluate_ppa_candidate(snapshot, MANIFEST, 0.3)["status"] == "PPA_PENDING"


def test_inferred_can_submit_never_counts_as_platform_confirmation():
    snapshot = ready_snapshot()
    snapshot["platform_can_submit"] = None
    snapshot["checks"] = []
    result = evaluate_ppa_candidate(snapshot, MANIFEST, 0.3)
    assert result["status"] == "PPA_PENDING"
    assert "PRE_SUBMIT_UNVERIFIED" in result["reasons"]


def test_actual_wq_shape_uses_classification_id_and_nested_theme_check():
    snapshot = ready_snapshot()
    snapshot["platform_can_submit"] = False
    snapshot["classifications"] = [{"id": "POWER_POOL_ELIGIBLE", "name": "Power Pool eligible"}]
    snapshot["themes"] = []
    snapshot["checks"] = [
        {"name": "MATCHES_THEMES", "result": "PASS",
         "themes": [{"id": "theme-2026-11"}]},
        {"name": "LOW_2Y_SHARPE", "result": "PASS", "value": 0.8},
    ]
    snapshot["metrics"].pop("two_year_sharpe")
    assert evaluate_ppa_candidate(snapshot, MANIFEST, 0.3)["status"] == "PPA_READY"


def test_correlation_above_platform_limit_is_rejected():
    result = evaluate_ppa_candidate(ready_snapshot(), MANIFEST, power_pool_corr=0.5)
    assert result["status"] == "PPA_REJECTED"
    assert "POWER_POOL_CORRELATION" in result["reasons"]


def test_missing_two_year_or_holdout_is_pending():
    snapshot = ready_snapshot()
    snapshot["metrics"].pop("two_year_sharpe")
    assert evaluate_ppa_candidate(snapshot, MANIFEST, 0.3)["status"] == "PPA_PENDING"
    snapshot = ready_snapshot()
    snapshot["metrics"].pop("test_sharpe")
    assert evaluate_ppa_candidate(snapshot, MANIFEST, 0.3)["status"] == "PPA_PENDING"


def test_failed_platform_check_is_rejected_even_if_can_submit_flag_is_true():
    snapshot = ready_snapshot()
    snapshot["checks"].append({"name": "SUB_UNIVERSE", "result": "FAIL"})
    assert evaluate_ppa_candidate(snapshot, MANIFEST, 0.3)["status"] == "PPA_REJECTED"


def test_unsupported_metrics_are_quarantined():
    snapshot = ready_snapshot()
    snapshot["metrics"]["sharpe"] = 173.76
    result = evaluate_ppa_candidate(snapshot, MANIFEST, 0.3)
    assert result["status"] == "PPA_PENDING"
    assert "METRIC_ANOMALY" in result["reasons"]


def test_turnover_and_concentration_gate():
    snapshot = ready_snapshot()
    snapshot["metrics"]["turnover"] = 0.8
    assert "TURNOVER_EXCESS" in evaluate_ppa_candidate(snapshot, MANIFEST, 0.3)["reasons"]
    snapshot = ready_snapshot()
    snapshot["metrics"]["long_count"] = 1
    assert "POSITION_CONCENTRATION" in evaluate_ppa_candidate(snapshot, MANIFEST, 0.3)["reasons"]


def test_low_turnover_cannot_be_ppa_ready():
    snapshot = ready_snapshot()
    snapshot["metrics"]["turnover"] = 0.005
    result = evaluate_ppa_candidate(snapshot, MANIFEST, 0.3)
    assert result["status"] == "PPA_REJECTED"
    assert "TURNOVER_BELOW_MINIMUM" in result["reasons"]


def test_explicit_amer_sharpe_must_exceed_theme_threshold():
    snapshot = ready_snapshot()
    snapshot["metrics"]["amer_sharpe"] = 1.0
    result = evaluate_ppa_candidate(snapshot, MANIFEST, 0.3)
    assert result["status"] == "PPA_REJECTED"
    assert "AMER_SHARPE_BELOW_THEME" in result["reasons"]
    snapshot["metrics"]["amer_sharpe"] = 1.01
    assert evaluate_ppa_candidate(snapshot, MANIFEST, 0.3)["status"] == "PPA_READY"


def test_platform_amer_check_value_is_used_when_metric_is_absent():
    snapshot = ready_snapshot()
    snapshot["metrics"].pop("amer_sharpe")
    snapshot["checks"].append({"name": "LOW_GLB_AMER_SHARPE",
                               "result": "WARNING", "value": 0.62})
    result = evaluate_ppa_candidate(snapshot, MANIFEST, 0.3)
    assert result["status"] == "PPA_REJECTED"
    assert "AMER_SHARPE_BELOW_THEME" in result["reasons"]


def test_theme_check_pass_does_not_replace_missing_amer_measurement():
    snapshot = ready_snapshot()
    snapshot["metrics"].pop("amer_sharpe")
    assert "amer_sharpe" not in snapshot["metrics"]
    assert evaluate_ppa_candidate(snapshot, MANIFEST, 0.3)["status"] == "PPA_PENDING"


def test_audit_snapshot_preserves_safe_ppa_evidence():
    snapshot = safe_result({
        **ready_snapshot(), "alpha_id": "WQ1", "success": True,
        "metrics": {**ready_snapshot()["metrics"], "amer_sharpe": 1.2},
        "classifications": [{"name": "POWER_POOL", "value": "POWER_POOL_ELIGIBLE"},
                            {"name": "sk-secret", "value": "sk-secret"}],
    })
    assert snapshot["classifications"] == [{"name": "POWER_POOL", "value": "POWER_POOL_ELIGIBLE"}]
    assert snapshot["themes"] == [{"id": "theme-2026-11"}]
    assert snapshot["metrics"]["test_sharpe"] == 0.6
    assert snapshot["metrics"]["amer_sharpe"] == 1.2
    assert "sk-secret" not in str(snapshot)


def test_audit_preserves_nested_theme_and_classification_id():
    snapshot = safe_result({**ready_snapshot(), "success": True,
        "classifications": [{"id": "POWER_POOL_ELIGIBLE", "name": "Power Pool eligible"}],
        "checks": [{"name": "MATCHES_THEMES", "result": "PASS",
                    "themes": [{"id": "theme-2026-11"}]}]})
    assert snapshot["classifications"][0]["id"] == "POWER_POOL_ELIGIBLE"
    assert snapshot["checks"][0]["themes"] == [{"id": "theme-2026-11"}]


def test_candidate_public_exposes_ppa_evaluation_without_raw_snapshot():
    row = SimpleNamespace(
        id=1, run_id=2, family_id="theme", source="MATRIX", arm="CONTROL",
        experiment_group="level", pair_id="p1", window=5,
        parent_candidate_id=None, expression="rank(a)", expression_hash="h1",
        settings_hash="h2", settings={}, validation_status="VALID",
        rejection_stage=None, rejection_reason=None, request_status="RESULT",
        wq_alpha_id="WQ1", quality_status="OPTIMIZE", gate_failures=[],
        metrics={}, trace_step_id=None, created_at=None,
        result_snapshot={"ppa_evaluation": {"status": "PPA_READY", "reasons": []},
                         "raw_response": "private"},
    )
    public = candidate_public(row)
    assert public["ppa_evaluation"]["status"] == "PPA_READY"
    assert "raw_response" not in str(public)


@pytest.mark.asyncio
async def test_power_pool_correlation_uses_dedicated_endpoint():
    adapter = BrainAdapter.__new__(BrainAdapter)
    adapter._safe_api_call = AsyncMock(return_value=httpx.Response(200, json={"max": 0.3}))
    result = await adapter.check_power_pool_correlation("WQ1")
    assert result["max"] == 0.3
    assert adapter._safe_api_call.await_args.args == ("get", "/alphas/WQ1/correlations/power-pool")


@pytest.mark.asyncio
async def test_mcp_adapter_does_not_route_ppa_corr_through_prod_tool():
    adapter = MCPBrainAdapter.__new__(MCPBrainAdapter)
    fallback = SimpleNamespace(check_power_pool_correlation=AsyncMock(return_value={"max": 0.2}))
    adapter._ensure_fallback = AsyncMock(return_value=fallback)
    result = await adapter.check_power_pool_correlation("WQ1")
    assert result["max"] == 0.2
    fallback.check_power_pool_correlation.assert_awaited_once_with("WQ1")
