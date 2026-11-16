"""Read-only, host-side exclusions covering every persisted research source."""
from __future__ import annotations

import re
from sqlalchemy import select

from backend.models import Alpha, ExperimentRun, ResearchCandidate
from backend.services.ppa_feedback import digest
from backend.services.ppa_isolation import isolated_signature


SETTINGS_KEYS = {"region", "universe", "delay", "decay", "neutralization", "truncation", "test_period"}
HASH = re.compile(r"[0-9a-f]{64}")


def expression_hash(expression):
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("HISTORY_EXPRESSION_MISSING")
    return digest("".join(expression.lower().split()))


def build_history(candidates, alphas, *, source):
    """Incomplete legacy settings exclude the expression under every setting."""
    pairs, expressions, identities = set(), set(), []
    for kind, rows in (("candidate", candidates), ("alpha", alphas)):
        for row in rows:
            value = row if isinstance(row, dict) else vars(row)
            expression = value.get("expression")
            key = expression_hash(expression)
            settings = dict(value.get("settings") or {})
            if "testPeriod" in settings and "test_period" not in settings:
                settings["test_period"] = settings["testPeriod"]
            complete = all(settings.get(name) is not None for name in SETTINGS_KEYS)
            if complete:
                try:
                    pair = isolated_signature(expression, settings)
                except (TypeError, ValueError, OverflowError):
                    complete = False
            if complete:
                pairs.add((pair["expression_hash"], pair["settings_hash"]))
                identity = pair
            else:
                expressions.add(key)
                identity = {"expression_hash": key, "all_settings": True}
            identities.append([kind, value.get("id"), identity])
    payload = {"version": 1, "source": source, "complete": True,
               "candidate_count": len(candidates), "alpha_count": len(alphas),
               "rows_sha256": digest(identities),
               "signatures": [dict(zip(("expression_hash", "settings_hash"), pair)) for pair in sorted(pairs)],
               "expression_hashes": sorted(expressions)}
    return {**payload, "history_sha256": digest(payload)}


def validate_history(value, *, source=None):
    keys = {"version", "source", "complete", "candidate_count", "alpha_count", "rows_sha256",
            "signatures", "expression_hashes", "history_sha256"}
    if (not isinstance(value, dict) or set(value) != keys or value.get("version") != 1
            or value.get("complete") is not True or value.get("source") not in {"production", "lab"}
            or (source is not None and value["source"] != source)
            or any(type(value.get(k)) is not int or value[k] < 0 for k in ("candidate_count", "alpha_count"))
            or not isinstance(value.get("signatures"), list)
            or not isinstance(value.get("expression_hashes"), list)
            or not HASH.fullmatch(str(value.get("rows_sha256")))
            or value.get("history_sha256") != digest({k: v for k, v in value.items() if k != "history_sha256"})):
        raise ValueError("HISTORY_EXCLUSIONS_INVALID")
    for pair in value["signatures"]:
        if (not isinstance(pair, dict) or set(pair) != {"expression_hash", "settings_hash"}
                or any(not isinstance(item, str) or not HASH.fullmatch(item) for item in pair.values())):
            raise ValueError("HISTORY_EXCLUSIONS_INVALID")
    if any(not isinstance(item, str) or not HASH.fullmatch(item) for item in value["expression_hashes"]):
        raise ValueError("HISTORY_EXCLUSIONS_INVALID")
    if len(value["signatures"]) + len(value["expression_hashes"]) > value["candidate_count"] + value["alpha_count"]:
        raise ValueError("HISTORY_EXCLUSIONS_INVALID")
    return value


def validate_global_history(value):
    if not isinstance(value, dict) or set(value) != {"production", "lab"}:
        raise ValueError("HISTORY_EXCLUSIONS_INVALID")
    for source in value:
        validate_history(value[source], source=source)
    return value


def history_blocked(value, expression, settings):
    pair = isolated_signature(expression, settings)
    return any(pair in item["signatures"] or pair["expression_hash"] in item["expression_hashes"]
               for item in validate_global_history(value).values())


def history_gate(frozen, live, expression=None, settings=None):
    try:
        validate_global_history(frozen)
        validate_global_history(live)
    except ValueError:
        return "HISTORY_EXCLUSIONS_INVALID"
    if any(frozen[source]["history_sha256"] != live[source]["history_sha256"] for source in frozen):
        return "HISTORY_EXCLUSIONS_CHANGED"
    if expression is not None and history_blocked(live, expression, settings):
        return "HISTORICAL_SIGNATURE"
    return "READY"


async def export_history(db, *, source, campaign_id=None):
    """Exclude only the run carrying this campaign's persisted identity in the lab."""
    excluded = []
    if campaign_id is not None:
        if source != "lab":
            raise ValueError("HISTORY_SCOPE_INVALID")
        runs = (await db.execute(select(ExperimentRun))).scalars().all()
        excluded = [r.id for r in runs if (r.checkpoint or {}).get("campaign_id") == campaign_id]
        if len(excluded) > 1:
            raise ValueError("DUPLICATE_CAMPAIGN_RUN")
    candidates = (await db.execute(select(ResearchCandidate).order_by(ResearchCandidate.id))).scalars().all()
    alphas = (await db.execute(select(Alpha).order_by(Alpha.id))).scalars().all()
    return build_history([row for row in candidates if row.run_id not in excluded],
                         [row for row in alphas if row.run_id not in excluded], source=source)
