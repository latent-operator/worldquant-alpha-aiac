"""Expression validation and correlation I/O around the pure quality policy."""
from functools import partial
from typing import Dict, List, Tuple

from loguru import logger

from backend.alpha_scoring import evaluate_with_brain_checks
from backend.services.alpha_quality_policy import (
    ExpressionFacts, final_quality_decision, local_quality_decision,
    operator_call_count, optimization_thresholds, strict_gate_failures as policy_gate_failures,
)


def expression_components(expression: str, fields: List[Dict]) -> Tuple[List[str], List[str]]:
    """Extract field/operator usage for hard complexity gates."""
    if not expression:
        return [], []

    try:
        from backend.alpha_semantic_validator import AlphaSemanticValidator

        validator = AlphaSemanticValidator(fields=fields, strict_field_check=False)
        result = validator.validate(expression)
        return sorted(result.used_fields), sorted(op.lower() for op in result.used_operators)
    except Exception:
        import re

        ops = sorted({m.group(1).lower() for m in re.finditer(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", expression)})
        field_ids = {str(f.get("id") or f.get("name") or "").lower() for f in fields}
        used_fields = sorted(
            {
                token
                for token in re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b", expression)
                if token.lower() in field_ids
            }
        )
        return used_fields, ops


def expression_facts(expression, fields):
    used_fields, used_operators = expression_components(expression, fields)
    return ExpressionFacts(tuple(used_fields), tuple(used_operators), operator_call_count(expression))


def strict_gate_failures(metrics, brain_failed_checks, prod_corr, self_corr, thresholds,
                         expression="", fields=None):
    """Compatibility boundary for callers that still supply an expression."""
    return policy_gate_failures(metrics, brain_failed_checks, prod_corr, self_corr, thresholds,
                                expression_facts(expression, fields or []))


def evaluator_for(brain):
    """Bind only the adapter, without constructing an Agent or owning its session."""
    return partial(evaluate_alpha_quality, brain=brain)


async def evaluate_alpha_quality(*, brain, task, expression, metrics, fields, alpha_id):
    thresholds = optimization_thresholds(task.config or {})
    sim_result = {
        "is": {
            "sharpe": metrics.get("sharpe", 0),
            "fitness": metrics.get("fitness", 0),
            "turnover": metrics.get("turnover", 0),
            "drawdown": metrics.get("drawdown", 0),
            "margin": metrics.get("margin", 0),
            "checks": metrics.get("checks", []),
        },
        "riskNeutralized": metrics.get("riskNeutralized", {}),
        "investabilityConstrained": metrics.get("investabilityConstrained", {}),
        "checks": metrics.get("checks", []),
        "can_submit": metrics.get("can_submit", False),
    }
    brain_eval = evaluate_with_brain_checks(sim_result)
    facts = expression_facts(expression, fields)
    decision = local_quality_decision(metrics, brain_eval, thresholds, facts, alpha_id)
    if decision is not None:
        metrics.update(decision.metrics_patch)
        return decision.status

    prod_corr = None
    self_corr = None
    try:
        reply = await brain.check_correlation(alpha_id, check_type="SELF")
        if isinstance(reply, dict) and reply.get("max") is not None:
            self_corr = float(reply["max"])
    except Exception as exc:
        logger.warning(f"[AlphaQuality] SELF correlation failed for {alpha_id}: {exc}")

    if self_corr is not None and self_corr < thresholds["self_corr_max"]:
        try:
            reply = await brain.check_correlation(alpha_id, check_type="PROD")
            if isinstance(reply, dict) and reply.get("max") is not None:
                prod_corr = float(reply["max"])
        except Exception as exc:
            logger.warning(f"[AlphaQuality] PROD correlation failed for {alpha_id}: {exc}")

    decision = final_quality_decision(metrics, brain_eval, thresholds, facts, prod_corr, self_corr)
    metrics.update(decision.metrics_patch)
    return decision.status
