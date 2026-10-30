"""Local research decisions, deliberately separate from platform eligibility."""
from collections import defaultdict
from math import isfinite, sqrt


def number(value):
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def evidence_metrics(row):
    metrics = dict(row.get("metrics") or {})
    snapshot = row.get("result_snapshot") or row
    if metrics.get("amer_sharpe") is None:
        metrics["amer_sharpe"] = next((c.get("value") for c in snapshot.get("checks", [])
                                      if c.get("name") == "LOW_GLB_AMER_SHARPE"), None)
    if metrics.get("test_sharpe") is None:
        metrics["test_sharpe"] = metrics.get("os_sharpe")
    return {key: number(value) for key, value in metrics.items() if not isinstance(value, dict)}


def seed_decision(row):
    metrics = evidence_metrics(row)
    reasons = []
    for key in ("sharpe", "fitness", "test_sharpe"):
        if metrics.get(key) is None or metrics[key] <= 0:
            reasons.append(f"NONPOSITIVE_OR_MISSING_{key.upper()}")
    for key in ("amer_sharpe", "turnover", "margin", "long_count", "short_count"):
        if metrics.get(key) is None:
            reasons.append(f"MISSING_{key.upper()}")
    snapshot = row.get("result_snapshot") or row
    evaluation = snapshot.get("platform_ppa_evaluation") or {}
    allowed = {"AMER_SHARPE_BELOW_THEME", "POWER_POOL_CLASSIFICATION", "THEME_UNVERIFIED",
               "THEME_CHECK_PENDING", "PLATFORM_CHECK_PENDING", "PRE_SUBMIT_UNVERIFIED",
               "POWER_POOL_CORRELATION_MISSING"}
    reasons.extend(reason for reason in evaluation.get("reasons", []) if reason not in allowed)
    if any("INELIGIBLE" in str(item).upper() for item in snapshot.get("classifications", [])):
        reasons.append("POWER_POOL_CLASSIFICATION_INELIGIBLE")
    if any(c.get("result") == "FAIL" for c in snapshot.get("checks", [])):
        reasons.append("PLATFORM_CHECK_FAILED")
    if (metrics.get("turnover") is not None and not 0 < metrics["turnover"] <= 1
            or any(metrics.get(key) is not None and metrics[key] <= 0 for key in ("long_count", "short_count"))):
        reasons.append("RISK_ANOMALY")
    return {"eligible": not reasons, "reasons": sorted(set(reasons)), "source": "local_policy",
            "amer_gap": max(0, 1 - metrics["amer_sharpe"]) if metrics.get("amer_sharpe") is not None else None}


def effective_improvement(parent, child):
    if not seed_decision(child)["eligible"]:
        return False
    a, b = evidence_metrics(parent), evidence_metrics(child)
    return (all(a.get(key) is not None for key in ("amer_sharpe", "sharpe", "fitness"))
            and b["amer_sharpe"] - a["amer_sharpe"] >= 0.05 - 1e-9
            and all(b[key] >= a[key] for key in ("sharpe", "fitness")))


def select_seeds(rows, limit=3):
    eligible = [row for row in rows if seed_decision(row)["eligible"]]
    return sorted(eligible, key=lambda r: tuple(evidence_metrics(r)[key]
                  for key in ("amer_sharpe", "test_sharpe", "sharpe")), reverse=True)[:limit]


def confirmation_cases(parent, allowed_settings):
    base = parent["settings"]
    choices = [(key, value) for key in ("decay", "neutralization")
               for value in allowed_settings.get(key, []) if value != base.get(key)]
    # Prefer different dimensions; do not silently invent account permissions.
    choices.sort(key=lambda pair: (pair[0] != "decay", str(pair[1])))
    selected = []
    for pair in choices:
        if not selected or pair[0] != selected[0][0]:
            selected.append(pair)
        if len(selected) == 2:
            break
    if len(selected) < 2:
        raise ValueError("PPA_TWO_PERTURBATIONS_REQUIRED")
    return [{"case_id": f"{parent['case_id']}_CONFIRM_{index}",
             "parent_case_id": parent["case_id"], "parent_candidate_id": parent.get("id"),
             "expression": parent["expression"], "dataset_id": parent["dataset_id"],
             "mechanism_id": parent["mechanism_id"], "settings": {key: value},
             "arm": "CONFIRMATION", "changed_element": key}
            for index, (key, value) in enumerate(selected, 1)]


def stopped_families(outcomes):
    streak = defaultdict(int)
    for row in outcomes:
        family = row.get("mechanism_id")
        if family:
            streak[family] = 0 if row.get("effective_improvement") else streak[family] + 1
    return {family for family, count in streak.items() if count >= 4}


def daily_correlation(left, right, min_samples=50):
    def increments(series):
        days = sorted(series)
        return {(a, b): number(series[b]) - number(series[a]) for a, b in zip(days, days[1:])
                if number(series[a]) is not None and number(series[b]) is not None}
    a, b = increments(left), increments(right)
    dates = sorted(a.keys() & b.keys())
    result = {"correlation": None, "samples": len(dates), "source": "internal_daily_pnl",
              "interval": [dates[0][0], dates[-1][1]] if dates else None}
    if len(dates) < min_samples:
        return result
    x, y = [a[d] for d in dates], [b[d] for d in dates]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    denom = sqrt(sum((v-mx)**2 for v in x) * sum((v-my)**2 for v in y))
    if denom:
        result["correlation"] = max(-1, min(1, sum((v-mx)*(w-my) for v, w in zip(x, y)) / denom))
    return result
