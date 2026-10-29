"""Evaluate Power Pool readiness from explicit BRAIN evidence.

This result is independent of Alpha.quality_status, whose strict RA gates remain
unchanged. Missing or pending platform evidence never produces PPA_READY.
"""

from datetime import datetime, timezone
from math import isfinite


def _tokens(value):
    if isinstance(value, str):
        yield value.upper()
    elif isinstance(value, dict):
        for child in value.values():
            yield from _tokens(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _tokens(child)


def _positive(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return isfinite(number) and number > 0


def _explicit_platform_power_pool(classifications: list, can_submit: bool) -> bool:
    """Recognize only a positive Power Pool tag returned by BRAIN itself."""
    if not can_submit:
        return False
    positive_values = {"POWER_POOL", "POWER_POOL_ELIGIBLE", "PURE_POWER_POOL",
                       "ELIGIBLE", "PASS"}
    for item in classifications:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").upper()
        value = str(item.get("value") or "").upper()
        identity = str(item.get("id") or "").upper()
        if identity == "POWER_POOL" or (name == "POWER_POOL" and value in positive_values):
            return True
    return False


def evaluate_ppa_candidate(snapshot: dict, manifest: dict, power_pool_corr: float | None) -> dict:
    """Return READY only with matching theme, checks, correlation and holdout."""
    rejected, pending = [], []
    theme_id = str(manifest.get("theme_id") or "")
    classification_rows = snapshot.get("classifications") or []
    classifications = set(_tokens(classification_rows))
    themes = set(_tokens(snapshot.get("themes") or []))
    checks = snapshot.get("checks") or []
    metrics = snapshot.get("metrics") or {}

    if snapshot.get("stage") != "IS" or snapshot.get("status") != "UNSUBMITTED":
        pending.append("STAGE_OR_STATUS_UNVERIFIED")
    eligible = {str(value).upper() for value in manifest.get("eligible_classification_ids") or []}
    classified = bool(classifications.intersection(eligible)) or _explicit_platform_power_pool(
        classification_rows, snapshot.get("platform_can_submit") is True)
    if not classified:
        if any("INELIGIBLE" in token for token in classifications):
            rejected.append("POWER_POOL_CLASSIFICATION")
        else:
            pending.append("POWER_POOL_CLASSIFICATION")
    theme_checks = [c for c in checks if str(c.get("name") or "").upper() == "MATCHES_THEMES"]
    matching_theme_checks = [c for c in theme_checks
                             if theme_id.upper() in set(_tokens(c.get("themes") or []))]
    if matching_theme_checks:
        themes.add(theme_id.upper())
    if not theme_id:
        pending.append("THEME_ID_MISSING")
    elif themes and theme_id.upper() not in themes:
        rejected.append("THEME_MISMATCH")
    elif not themes:
        pending.append("THEME_UNVERIFIED")

    if any(str(c.get("result") or "").upper() == "FAIL" for c in checks):
        rejected.append("PLATFORM_CHECK_FAILED")
    relevant_theme_checks = (matching_theme_checks if any(c.get("themes") for c in theme_checks)
                             else theme_checks)
    if not relevant_theme_checks or not any(str(c.get("result") or "").upper() == "PASS"
                                            for c in relevant_theme_checks):
        pending.append("THEME_CHECK_PENDING")
    if any(str(c.get("result") or "").upper() == "PENDING" for c in checks):
        pending.append("PLATFORM_CHECK_PENDING")
    required_checks = {str(name).upper() for name in manifest.get("required_pre_submit_checks") or []}
    checks_pass = bool(required_checks) and all(
        any(str(c.get("name") or "").upper() == name
            and str(c.get("result") or "").upper() == "PASS"
            for c in checks) for name in required_checks
    )
    if snapshot.get("platform_can_submit") is not True and not checks_pass:
        pending.append("PRE_SUBMIT_UNVERIFIED")

    limit = manifest.get("ppa_corr_max")
    try:
        corr = float(power_pool_corr)
        corr_limit = float(limit)
        if not isfinite(corr) or not isfinite(corr_limit):
            raise ValueError
        if corr >= corr_limit:
            rejected.append("POWER_POOL_CORRELATION")
    except (TypeError, ValueError):
        pending.append("POWER_POOL_CORRELATION_MISSING")

    two_year_value = metrics.get("two_year_sharpe")
    if two_year_value is None:
        two_year_value = next((c.get("value") for c in checks
                               if str(c.get("name") or "").upper() == "LOW_2Y_SHARPE"
                               and c.get("value") is not None), None)
    two_year = _positive(two_year_value)
    if two_year is None:
        pending.append("TWO_YEAR_MISSING")
    elif not two_year:
        rejected.append("TWO_YEAR_NONPOSITIVE")
    holdout_value = metrics.get("test_sharpe")
    if holdout_value is None:
        holdout_value = metrics.get("os_sharpe")
    holdout = _positive(holdout_value)
    if holdout is None:
        pending.append("HOLDOUT_MISSING")
    elif not holdout:
        rejected.append("HOLDOUT_NONPOSITIVE")
    try:
        sharpe = float(metrics.get("sharpe"))
        if not isfinite(sharpe) or abs(sharpe) > 10:
            pending.append("METRIC_ANOMALY")
    except (TypeError, ValueError):
        pending.append("IS_SHARPE_MISSING")

    amer_threshold = (manifest.get("theme_requirements") or {}).get(
        "amer_sharpe_strictly_greater_than")
    amer_value = metrics.get("amer_sharpe")
    if amer_value is None:
        amer_value = next((check.get("value") for check in checks
                           if str(check.get("name") or "").upper() == "LOW_GLB_AMER_SHARPE"
                           and check.get("value") is not None), None)
    if amer_threshold is not None and amer_value is not None:
        try:
            amer_sharpe = float(amer_value)
            if not isfinite(amer_sharpe):
                pending.append("AMER_SHARPE_ANOMALY")
            elif amer_sharpe <= float(amer_threshold):
                rejected.append("AMER_SHARPE_BELOW_THEME")
        except (TypeError, ValueError):
            pending.append("AMER_SHARPE_ANOMALY")

    try:
        turnover = float(metrics["turnover"])
        turnover_min = float(manifest.get("turnover_min", 0.01))
        turnover_max = float(manifest["turnover_max"])
        if not isfinite(turnover) or turnover < 0:
            pending.append("TURNOVER_ANOMALY")
        elif turnover < turnover_min:
            rejected.append("TURNOVER_BELOW_MINIMUM")
        elif turnover > turnover_max:
            rejected.append("TURNOVER_EXCESS")
    except (KeyError, TypeError, ValueError):
        pending.append("TURNOVER_UNVERIFIED")
    try:
        minimum = int(manifest["min_positions"])
        counts = (int(metrics["long_count"]), int(metrics["short_count"]))
        if min(counts) < minimum:
            rejected.append("POSITION_CONCENTRATION")
    except (KeyError, TypeError, ValueError):
        pending.append("POSITION_CONCENTRATION_UNVERIFIED")

    status = "PPA_REJECTED" if rejected else "PPA_PENDING" if pending else "PPA_READY"
    return {
        "status": status,
        "reasons": sorted(set(rejected + pending)),
        "theme_id": theme_id or None,
        "check_source": "WQ_ALPHA_DETAILS_AND_POWER_POOL_CORRELATION",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
