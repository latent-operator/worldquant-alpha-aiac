"""Deterministic Alpha quality gates with no runtime or network dependencies.

Threshold sources remain caller-owned. Expression facts are computed before the
policy is called, so semantic validation cannot import database effects here.
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ExpressionFacts:
    used_fields: tuple[str, ...]
    used_operators: tuple[str, ...]
    operator_count: int


@dataclass(frozen=True)
class QualityDecision:
    status: str
    metrics_patch: dict


def operator_call_count(expression: str) -> int:
    """Count operator calls, not unique operator names, for hard complexity gates."""
    if not expression:
        return 0
    import re

    return len(re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\s*\(", expression))


def as_float(value, default: float = 0.0) -> float:
    """Convert numeric API values defensively."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def risk_neutralized_metrics(metrics: Dict) -> Dict[str, Any]:
    """Normalize nested and flat MCP/BRAIN risk-neutralized metric shapes."""
    rn_metrics = metrics.get("riskNeutralized") or metrics.get("risk_neutralized") or {}
    if not isinstance(rn_metrics, dict):
        rn_metrics = {}

    sharpe = (
        rn_metrics.get("sharpe")
        if rn_metrics.get("sharpe") is not None
        else metrics.get("risk_neutralized_sharpe", metrics.get("rn_sharpe"))
    )
    fitness = (
        rn_metrics.get("fitness")
        if rn_metrics.get("fitness") is not None
        else metrics.get("risk_neutralized_fitness", metrics.get("rn_fitness"))
    )

    normalized = dict(rn_metrics)
    if sharpe is not None:
        normalized["sharpe"] = sharpe
    if fitness is not None:
        normalized["fitness"] = fitness
    if "sharpe" not in normalized or "fitness" not in normalized:
        sim_settings = metrics.get("_settings") or metrics.get("settings") or {}
        neutralization = str(sim_settings.get("neutralization") or "").upper()
        if neutralization and neutralization != "NONE":
            if "sharpe" not in normalized and metrics.get("sharpe") is not None:
                normalized["sharpe"] = metrics.get("sharpe")
            if "fitness" not in normalized and metrics.get("fitness") is not None:
                normalized["fitness"] = metrics.get("fitness")
            normalized.setdefault("_source", f"neutralized_setting:{neutralization}")
    return normalized


def check_value(metrics: Dict, check_name: str) -> Any:
    """Return a BRAIN check value when present."""
    for check in metrics.get("checks") or []:
        if isinstance(check, dict) and check.get("name") == check_name:
            return check.get("value")
    return None


def strict_gate_failures(
    metrics: Dict,
    brain_failed_checks: List[str],
    prod_corr: Optional[float],
    self_corr: Optional[float],
    thresholds: Dict[str, Any],
    expression_facts: ExpressionFacts,
) -> List[str]:
    """Production-quality gates requested for mined deliverables."""
    sharpe = as_float(metrics.get("sharpe"))
    two_year_value = metrics.get("two_year_sharpe")
    if two_year_value is None:
        two_year_value = check_value(metrics, "LOW_2Y_SHARPE")
    two_year_sharpe = as_float(two_year_value, sharpe)
    fitness = as_float(metrics.get("fitness"))
    margin = as_float(metrics.get("margin"))
    turnover = as_float(metrics.get("turnover"))
    rn_metrics = risk_neutralized_metrics(metrics)
    rn_sharpe = as_float(rn_metrics.get("sharpe"), default=None)
    rn_fitness = as_float(rn_metrics.get("fitness"), default=None)
    ra_fails = len(brain_failed_checks or [])

    sharpe_min = thresholds["sharpe_min"]
    two_year_sharpe_min = thresholds["two_year_sharpe_min"]
    fitness_min = thresholds["fitness_min"]
    rn_sharpe_min = thresholds["rn_sharpe_min"]
    rn_fitness_min = thresholds["rn_fitness_min"]
    margin_min = thresholds["margin_min"]
    turnover_min = thresholds["turnover_min"]
    turnover_max = thresholds["turnover_max"]
    prod_corr_max = thresholds["prod_corr_max"]
    self_corr_max = thresholds["self_corr_max"]
    ra_fails_max = thresholds["ra_fails_max"]
    max_operator_count = int(thresholds.get("max_operator_count", 7))

    failures = []
    used_fields, used_ops = expression_facts.used_fields, expression_facts.used_operators
    stage = metrics.get("stage")
    status = metrics.get("status")
    if "trade_when" in {op.lower() for op in used_ops}:
        failures.append("TRADE_WHEN_FORBIDDEN")
    if len(used_fields) > 2:
        failures.append(f"TOO_MANY_FIELDS (fields={len(used_fields)} > 2)")
    operator_calls = expression_facts.operator_count
    if operator_calls > max_operator_count:
        failures.append(f"TOO_MANY_OPERATORS (ops={operator_calls} > {max_operator_count})")
    if stage != "IS":
        failures.append(f"NOT_IS_STAGE (stage={stage if stage is not None else 'missing'})")
    if status != "UNSUBMITTED":
        failures.append(f"NOT_UNSUBMITTED (status={status if status is not None else 'missing'})")
    if sharpe <= sharpe_min:
        failures.append(f"LOW_SHARPE (is={sharpe:.2f} <= {sharpe_min:.2f})")
    if two_year_sharpe <= two_year_sharpe_min:
        failures.append(f"LOW_2Y_SHARPE (2y={two_year_sharpe:.2f} <= {two_year_sharpe_min:.2f})")
    if fitness <= fitness_min:
        failures.append(f"LOW_FITNESS (fit={fitness:.2f} <= {fitness_min:.2f})")
    if rn_sharpe is None or rn_sharpe <= rn_sharpe_min:
        failures.append(f"LOW_RN_SHARPE (rn={rn_sharpe if rn_sharpe is not None else 'missing'} <= {rn_sharpe_min:.2f})")
    if rn_fitness is None or rn_fitness <= rn_fitness_min:
        failures.append(f"LOW_RN_FITNESS (rn={rn_fitness if rn_fitness is not None else 'missing'} <= {rn_fitness_min:.2f})")
    if margin <= margin_min:
        failures.append(f"LOW_MARGIN (margin={margin:.6f} <= {margin_min:.6f})")
    if turnover <= turnover_min:
        failures.append(f"LOW_TURNOVER (to={turnover:.3f} <= {turnover_min:.3f})")
    if turnover >= turnover_max:
        failures.append(f"HIGH_TURNOVER (to={turnover:.3f} >= {turnover_max:.3f})")
    if ra_fails > ra_fails_max:
        failures.append(f"RA_FAILS (fails={ra_fails} > {ra_fails_max})")
    if prod_corr is None:
        failures.append("PROD_CORR_MISSING")
    elif prod_corr >= prod_corr_max:
        failures.append(f"HIGH_PROD_CORR (pc={prod_corr:.3f} >= {prod_corr_max:.3f})")
    if self_corr is None:
        failures.append("SELF_CORR_MISSING")
    elif self_corr >= self_corr_max:
        failures.append(f"HIGH_SELF_CORR (sc={self_corr:.3f} >= {self_corr_max:.3f})")

    return failures


def optimization_thresholds(config: dict) -> dict:
    """Preserve optimization task overrides, separate from graph DB defaults."""
    return {
        "sharpe_min": float(config.get("sharpe_min", 1.58)),
        "two_year_sharpe_min": float(config.get("two_year_sharpe_min", 1.6)),
        "fitness_min": float(config.get("fitness_min", 1.0)),
        "rn_sharpe_min": float(config.get("rn_sharpe_min", 1.58)),
        "rn_fitness_min": float(config.get("rn_fitness_min", 1.0)),
        "margin_min": float(config.get("margin_min", 0.001)),
        "turnover_min": float(config.get("turnover_min", 0.05)),
        "turnover_max": float(config.get("turnover_max", 0.30)),
        "prod_corr_max": float(config.get("prod_corr_max", 0.7)),
        "self_corr_max": float(config.get("self_corr_max", 0.5)),
        "ra_fails_max": int(config.get("ra_fails_max", 0)),
        "max_operator_count": int(config.get("max_operator_count", 5)),
    }


def local_quality_decision(metrics, brain_evaluation, thresholds, expression_facts, alpha_id):
    """Return a local rejection, or None when correlation evidence is needed."""
    failures = strict_gate_failures(metrics, brain_evaluation.get("failed_checks", []),
                                    0.0, 0.0, thresholds, expression_facts)
    failures = [failure for failure in failures
                if failure not in {"PROD_CORR_MISSING", "SELF_CORR_MISSING"}
                and not failure.startswith("HIGH_PROD_CORR")
                and not failure.startswith("HIGH_SELF_CORR")]
    if failures or not alpha_id:
        return QualityDecision("OPTIMIZE", {"_strict_gate_failures": failures,
                                            "_hard_pass": False, "_corr_checked": False})
    return None


def final_quality_decision(metrics, brain_evaluation, thresholds, expression_facts,
                           prod_corr, self_corr):
    failures = strict_gate_failures(metrics, brain_evaluation.get("failed_checks", []),
                                    prod_corr, self_corr, thresholds, expression_facts)
    return QualityDecision("PASS" if not failures else "OPTIMIZE", {
        "_prod_corr": round(prod_corr, 4) if prod_corr is not None else None,
        "_self_corr": round(self_corr, 4) if self_corr is not None else None,
        "_corr_checked": True, "_strict_gate_failures": failures, "_hard_pass": not failures,
        "_brain_failed_checks": brain_evaluation.get("failed_checks", []),
        "_brain_pending_checks": brain_evaluation.get("pending_checks", []),
    })
