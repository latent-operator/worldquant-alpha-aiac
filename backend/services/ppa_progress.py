"""Auditable progress, never a prediction of time-to-PASS."""
from collections import Counter
from datetime import datetime
from backend.services.ppa_feedback import research_row
from backend.services.ppa_research_policy import evidence_metrics, effective_improvement


def progress_report(rows, current_run_id):
    raw = [row if isinstance(row, dict) else {key: getattr(row, key, None) for key in (
        "id", "run_id", "request_status", "source", "arm", "pair_id", "metrics", "result_snapshot",
        "parent_candidate_id", "experiment_group", "settings", "expression")} for row in rows]
    measured = [research_row(row) for row in raw if row["request_status"] in {"RESULT", "CACHED"}]
    by_id = {row["id"]: row for row in measured}
    deltas, gains = [], 0
    for row in measured:
        parent = by_id.get(row.get("parent_candidate_id"))
        if not parent:
            continue
        a, b = evidence_metrics(parent), evidence_metrics(row)
        deltas.append({"parent_id": parent["id"], "candidate_id": row["id"],
                       **{key: b[key] - a[key] if b.get(key) is not None and a.get(key) is not None else None
                          for key in ("amer_sharpe", "sharpe", "fitness", "test_sharpe")}})
        gains += row.get("run_id") == current_run_id and effective_improvement(parent, row)
    new = [row for row in raw if row.get("run_id") == current_run_id and row["request_status"] in {
        "RESERVED", "REQUESTED", "UNKNOWN", "FAILED", "RESULT"}]
    positive = [row for row in measured if (row["metrics"].get("test_sharpe") or 0) > 0
                and row["metrics"].get("amer_sharpe") is not None]
    best = max(positive, key=lambda row: row["metrics"]["amer_sharpe"], default=None)
    missing = Counter(reason for row in measured for reason in row["platform_ppa_evaluation"].get("reasons", []))
    durations = []
    for row in new:
        request = (row.get("result_snapshot") or {}).get("request") or {}
        try:
            durations.append((datetime.fromisoformat(request["finished_at"]) - datetime.fromisoformat(request["sent_at"])).total_seconds())
        except (KeyError, TypeError, ValueError):
            pass
    return {"new_requests": len(new), "effective_improvements": gains,
            "per_ten_requests": gains / len(new) * 10 if len(new) >= 10 else None,
            "parent_deltas": deltas, "best_amer_positive_test": {"candidate_id": best["id"],
                "amer_sharpe": best["metrics"]["amer_sharpe"],
                "gap": max(0, 1 - best["metrics"]["amer_sharpe"])} if best else None,
            "ppa_ready": sum(row["ppa_evaluation"].get("status") == "PPA_READY" for row in measured),
            "evidence_reasons": dict(missing), "cached_results": sum(r["request_status"] == "CACHED" for r in raw),
            "timing": {"wq_seconds": sum(durations) if durations else None, "timed_requests": len(durations),
                       "generation_seconds": None, "qualification_seconds": None, "pause_seconds": None,
                       "recovery_seconds": None}, "speed_claim": "NO_TIME_TO_PASS_ESTIMATE"}
