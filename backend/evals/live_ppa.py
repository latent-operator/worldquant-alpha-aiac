"""Pre-register a bounded PPA selector comparison before any WQ result exists."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from math import ceil
from random import Random
import re

from backend.alpha_semantic_validator import AlphaSemanticValidator
from backend.services.ppa_feedback import (
    _lookback_slots, _measured_lookbacks, behavioral_signature, case_settings,
    digest, freeze_batch, select_local_seeds,
)
from backend.services.ppa_research_policy import evidence_metrics
from backend.services.research_audit import signature


def evidence_key(parent: dict) -> tuple:
    """Only previously persisted parent evidence enters this pre-WQ ranking."""
    m = evidence_metrics(parent)
    value = lambda key: float(m.get(key) or 0)
    count = sum((value("sharpe") >= 1.58, value("fitness") >= 1.0,
                 value("test_sharpe") > 0, value("amer_sharpe") >= 1.0,
                 0.05 <= value("turnover") <= 0.30, value("margin") >= 0.001))
    return (count, value("fitness"), value("test_sharpe"),
            value("amer_sharpe"), value("sharpe"),
            digest([parent.get("expression"), parent.get("settings") or {}, parent["id"]]))


def policy_orders(candidates: list[dict], baseline_parents: list[dict]) -> tuple[list[str], list[str]]:
    """Rank one child per parent before repeating parents; ignore child outcomes."""
    grouped: dict[int, list[str]] = defaultdict(list)
    for case in candidates:
        grouped[int(case["parent_candidate_id"])].append(case["case_id"])
    for cases in grouped.values():
        cases.sort(key=lambda case_id: digest(["child-tie", case_id]))

    def order(parents):
        result = []
        for round_index in range(max(map(len, grouped.values()), default=0)):
            result.extend(grouped[parent["id"]][round_index]
                          for parent in parents
                          if parent["id"] in grouped and len(grouped[parent["id"]]) > round_index)
        return result

    baseline = order(baseline_parents)
    evidence = order(sorted(baseline_parents, key=evidence_key, reverse=True))
    if set(baseline) != set(evidence) or len(baseline) != len(candidates):
        raise ValueError("POLICY_ORDER_INCOMPLETE")
    return baseline, evidence


def _variant_options(parent: dict, base: dict, lookbacks: dict) -> list[tuple[str, str]]:
    expression = parent["expression"]
    variants = []
    for start, end, original, operator in _lookback_slots(expression):
        for replacement in lookbacks.get(operator, []):
            if replacement != original:
                variants.append((expression[:start] + str(replacement) + expression[end:],
                                 f"measured {operator} lookback {original} -> {replacement}"))
    dataset = parent["dataset_id"]
    fields = base["datasets"][dataset]
    metadata = (base.get("field_metadata") or {}).get(dataset) or {}
    validator = AlphaSemanticValidator(fields=[{"id": name, "type": (metadata.get(name) or {}).get("type", "MATRIX")}
                                               for name in fields], strict_field_check=True)
    used = set(validator.validate(expression).used_fields)
    if len(used) == 1:
        old = next(iter(used))
        for new in fields:
            if new != old:
                variants.append((re.sub(rf"\b{re.escape(old)}\b", new, expression),
                                 f"field {old} -> {new}"))
    return sorted(set(variants), key=lambda item: digest(["ppa-live-eval-v1", parent["id"], item[0]]))


def freeze_experiment(feedback: dict, *, source_run_id: int, source_task_id: int,
                      prepare_commit: str, prepared_at: str, budget: int = 80,
                      top_k: int = 20, min_exclusive: int = 8) -> dict:
    """Generate all cases and both rankings from one read-only source snapshot."""
    if not 1 <= top_k <= budget <= 80 or min_exclusive > top_k:
        raise ValueError("EXPERIMENT_SIZE_INVALID")
    base = feedback["base"]
    exhausted = {tuple(item) for item in feedback.get("exhausted_templates") or []}
    parents = select_local_seeds(feedback["rows"], base,
        max_operators=feedback.get("strict_max_operator_count") or 5,
        parent_total_counts=feedback.get("parent_total_counts") or {},
        exhausted=exhausted, limit=10000)
    evidence = {**feedback, "next_phase": "refinement", "phase_limit": budget,
                "phase_counts": {"refinement": 0}, "max_batch_cases": budget,
                "budget_remaining": None, "seeds": parents}
    lookbacks = _measured_lookbacks(feedback)
    settings = case_settings(base, {})
    used_signatures = {tuple(item) for item in feedback.get("history_signatures") or []}
    used_behavioral = {tuple(item) for item in feedback.get("history_behavioral_signatures") or []}
    parent_counts = feedback.get("parent_total_counts") or {}
    accepted = []
    selected_counts: dict[int, int] = defaultdict(int)
    for _round in range(4):
        for parent in sorted(parents, key=lambda item: item["id"]):
            if len(accepted) == budget:
                break
            parent_id = int(parent["id"])
            remaining = 4 - int(parent_counts.get(str(parent_id), parent_counts.get(parent_id, 0)))
            if selected_counts[parent_id] >= remaining:
                continue
            for expression, changed in _variant_options(parent, base, lookbacks):
                identity = signature(expression, settings)
                behavior = behavioral_signature(expression, settings)
                if identity in used_signatures or behavior in used_behavioral:
                    continue
                case = {"case_id": "E_" + digest([parent_id, expression])[:16],
                        "parent_case_id": parent["case_id"], "arm": "TREATMENT",
                        "dataset_id": parent["dataset_id"], "mechanism_id": parent["mechanism_id"],
                        "expression": expression, "changed_element": changed,
                        "expected_effect": "Measure the frozen parent hypothesis after one edit",
                        "risk": "The edit may lower quality",
                        "stop_condition": "No effective improvement"}
                try:
                    freeze_batch(base, evidence, {"phase": "refinement", "cases": [case]})
                except (ValueError, KeyError, TypeError):
                    continue
                accepted.append(case)
                selected_counts[parent_id] += 1
                used_signatures.add(identity)
                used_behavioral.add(behavior)
                break
        if len(accepted) == budget:
            break
    if len(accepted) != budget:
        raise ValueError(f"INSUFFICIENT_CANDIDATES:{len(accepted)}/{budget}")
    try:
        frozen = freeze_batch(base, evidence, {"phase": "refinement", "cases": accepted})
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("EXPERIMENT_BATCH_VALIDATION_FAILED") from exc
    cases = []
    for case in frozen["cases"]:
        identity = signature(case["expression"], settings)
        cases.append({**case, "settings": settings,
                      "expression_signature": list(identity),
                      "parent_source_candidate_id": case["parent_candidate_id"]})
    baseline, evidence_order = policy_orders(cases, parents)
    exclusive = len(set(evidence_order[:top_k]) - set(baseline[:top_k]))
    if exclusive < min_exclusive:
        raise ValueError(f"INSUFFICIENT_POLICY_CONTRAST:{exclusive}/{min_exclusive}")
    payload = {"schema_version": 1, "budget": budget, "top_k": top_k,
               "min_results": ceil(0.95 * budget),
               "source_task_id": source_task_id, "source_run_id": source_run_id,
               "source_evidence_hash": feedback.get("evidence_hash"),
               "source_commit": None, "prepare_commit": prepare_commit,
               "prepared_at": prepared_at, "base": base,
               "parent_evidence": {str(parent["id"]): {
                   "id": parent["id"], "case_id": parent["case_id"],
                   "expression": parent["expression"], "settings": parent.get("settings") or settings,
                   "dataset_id": parent["dataset_id"], "mechanism_id": parent["mechanism_id"],
                   "metrics": evidence_metrics(parent),
                   "checks": parent.get("checks") or [],
                   "platform_ppa_evaluation": parent.get("platform_ppa_evaluation") or {},
               } for parent in parents if parent["id"] in selected_counts},
               "candidates": cases, "baseline_order": baseline,
               "evidence_order": evidence_order,
               "send_order": sorted([case["case_id"] for case in cases],
                                    key=lambda case_id: digest(["send-v1", case_id, prepare_commit]))}
    return {**payload, "manifest_sha256": digest(payload)}


def dispatch_gate(state: dict) -> str:
    if not str(state.get("lab_database", "")).startswith("aiac_eval_") or state.get("lab_port") == 55433:
        return "LAB_DATABASE_REQUIRED"
    if state.get("source_unresolved") != 0:
        return "SOURCE_REQUEST_PENDING"
    if state.get("source_run_current") is False:
        return "SOURCE_RUN_CHANGED"
    if state.get("other_active_work"):
        return "OTHER_WQ_WORK_ACTIVE"
    if not state.get("platform_verified"):
        return "PLATFORM_UNVERIFIED"
    try:
        ends = datetime.fromisoformat(state["theme_ends_at"].replace("Z", "+00:00"))
        now = state["now"]
        if now >= ends or now.tzinfo is None:
            return "THEME_EXPIRED"
    except (KeyError, TypeError, ValueError):
        return "THEME_EXPIRED"
    if state.get("lab_unresolved", 0) or state.get("existing_requests", 0) >= state.get("budget", 0):
        return "LAB_REQUEST_PENDING_OR_EXHAUSTED"
    return "READY"


def analyze_results(manifest: dict, rows: list[dict]) -> dict:
    cases = {case["case_id"]: case for case in manifest["candidates"]}
    outcomes = {row["case_id"]: row for row in rows}
    if len(outcomes) != len(rows):
        raise ValueError("DUPLICATE_RESULT_CASE")
    if set(outcomes) - set(cases):
        raise ValueError("RESULT_CASE_UNKNOWN")
    pending = set(cases) - set(outcomes)
    pending.update(case_id for case_id, row in outcomes.items()
                   if row.get("request_status") in {"RESERVED", "REQUESTED", "UNKNOWN"})

    def summarize(order: list[str], limit: int) -> dict:
        chosen = order[:limit]
        sharpe = [float(outcomes[cid]["test_sharpe"]) for cid in chosen
                  if outcomes.get(cid, {}).get("request_status") == "RESULT"
                  and outcomes[cid].get("test_sharpe") is not None]
        return {"selected": len(chosen),
                "quality_pass": sum(outcomes.get(cid, {}).get("quality_status") == "PASS" for cid in chosen),
                "ppa_ready": sum(outcomes.get(cid, {}).get("ppa_status") == "PPA_READY" for cid in chosen),
                "effective_improvement": sum(outcomes.get(cid, {}).get("effective_improvement") is True
                                             for cid in chosen),
                "test_sharpe_count": len(sharpe),
                "test_sharpe_mean": sum(sharpe) / len(sharpe) if sharpe else None}

    top_k = manifest["top_k"]
    baseline = summarize(manifest["baseline_order"], top_k)
    evidence = summarize(manifest["evidence_order"], top_k)
    result_count = sum(row.get("request_status") == "RESULT" for row in outcomes.values())
    if pending:
        outcome = "INCOMPLETE_RECONCILIATION"
    elif result_count < manifest.get("min_results", ceil(0.95 * manifest["budget"])):
        outcome = "INSUFFICIENT_RESULTS"
    elif (evidence["quality_pass"] >= baseline["quality_pass"]
          and evidence["ppa_ready"] >= baseline["ppa_ready"]
          and (evidence["quality_pass"] > baseline["quality_pass"]
               or evidence["ppa_ready"] > baseline["ppa_ready"])):
        outcome = "OBSERVED_HARD_OUTCOME_UPLIFT"
    elif (all(item["quality_pass"] == item["ppa_ready"] == 0 for item in (baseline, evidence))
          and evidence["effective_improvement"] > baseline["effective_improvement"]):
        outcome = "PROXY_DIRECTIONAL_UPLIFT"
    else:
        outcome = "NO_DEMONSTRATED_IMPROVEMENT"
    sensitivity = {str(limit): {
        "baseline": summarize(manifest["baseline_order"], limit),
        "evidence": summarize(manifest["evidence_order"], limit),
    } for limit in (top_k, min(40, manifest["budget"]))}
    metric_names = ("quality_pass", "ppa_ready", "effective_improvement")
    difference = {name: evidence[name] - baseline[name] for name in metric_names}
    difference["test_sharpe_mean"] = (evidence["test_sharpe_mean"] - baseline["test_sharpe_mean"]
                                      if evidence["test_sharpe_mean"] is not None
                                      and baseline["test_sharpe_mean"] is not None else None)
    # Resample parent clusters. Shared candidates cancel within each replicate.
    by_parent: dict[int, list[str]] = defaultdict(list)
    for cid, case in cases.items():
        by_parent[int(case["parent_candidate_id"])].append(cid)
    parents = sorted(by_parent)
    rng = Random(manifest.get("manifest_sha256", "fixed-eval-seed"))
    intervals = {}
    if parents:
        selected_baseline = set(manifest["baseline_order"][:top_k])
        selected_evidence = set(manifest["evidence_order"][:top_k])
        samples = {name: [] for name in metric_names}
        for _ in range(2000):
            counts = {parent: 0 for parent in parents}
            for _draw in parents:
                counts[rng.choice(parents)] += 1
            for name in metric_names:
                key = {"quality_pass": "quality_status", "ppa_ready": "ppa_status",
                       "effective_improvement": "effective_improvement"}[name]
                wanted = {"quality_pass": "PASS", "ppa_ready": "PPA_READY",
                          "effective_improvement": True}[name]
                samples[name].append(sum(counts[parent] * sum(
                    (cid in selected_evidence) * (outcomes.get(cid, {}).get(key) == wanted)
                    - (cid in selected_baseline) * (outcomes.get(cid, {}).get(key) == wanted)
                    for cid in cids) for parent, cids in by_parent.items()))
        for name in metric_names:
            ordered = sorted(samples[name])
            intervals[name] = [ordered[49], ordered[1949]]
    status_counts = {status: sum(row.get("request_status") == status for row in outcomes.values())
                     for status in ("RESULT", "FAILED", "UNKNOWN", "REQUESTED", "RESERVED")}
    return {"outcome": outcome, "result_count": result_count,
            "unknown_count": status_counts["UNKNOWN"], "pending_count": len(pending),
            "request_status_counts": status_counts, "difference": difference,
            "confidence_intervals": intervals, "confidence_method": "parent_cluster_bootstrap_2000",
            "baseline": baseline, "evidence": evidence, "sensitivity": sensitivity}
