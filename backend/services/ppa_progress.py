"""Auditable progress, never a prediction of time-to-PASS."""
from collections import Counter
from datetime import datetime, timezone
from backend.services.ppa_feedback import research_row
from backend.services.ppa_research_policy import evidence_metrics, effective_improvement


def progress_report(rows, current_run_id, checkpoint=None):
    checkpoint = checkpoint or {}
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
    durations, recovered, recovery_durations, qualification_durations = [], 0, [], []
    for row in new:
        snapshot = row.get("result_snapshot") or {}
        request = snapshot.get("request") or {}
        recovery = snapshot.get("recovery") or {}
        if recovery:
            recovered += 1
            if isinstance(recovery.get("duration_seconds"), (int, float)):
                recovery_durations.append(recovery["duration_seconds"])
        if isinstance(snapshot.get("qualification_seconds"), (int, float)):
            qualification_durations.append(snapshot["qualification_seconds"])
        try:
            if row["request_status"] == "RESULT" and not recovery:
                durations.append((datetime.fromisoformat(request["finished_at"]) - datetime.fromisoformat(request["sent_at"])).total_seconds())
        except (KeyError, TypeError, ValueError):
            pass
    generations = [batch.get("candidate_generation", {}).get("generation_seconds") for batch in
                   [*(checkpoint.get("ppa_batch_history") or []), checkpoint.get("ppa_batch") or {}]]
    generations = [value for value in generations if isinstance(value, (int, float))]
    pause_seconds = checkpoint.get("ppa_pause_seconds")
    if checkpoint.get("ppa_pause_started_at"):
        try:
            active = (datetime.now(timezone.utc) - datetime.fromisoformat(
                checkpoint["ppa_pause_started_at"])).total_seconds()
            pause_seconds = (pause_seconds or 0) + max(0, active)
        except (TypeError, ValueError):
            pass
    return {"new_requests": len(new), "effective_improvements": gains,
            "per_ten_requests": gains / len(new) * 10 if len(new) >= 10 else None,
            "parent_deltas": deltas, "best_amer_positive_test": {"candidate_id": best["id"],
                "amer_sharpe": best["metrics"]["amer_sharpe"],
                "gap": max(0, 1 - best["metrics"]["amer_sharpe"])} if best else None,
            "ppa_ready": sum(row["ppa_evaluation"].get("status") == "PPA_READY" for row in measured),
            "evidence_reasons": dict(missing), "cached_results": sum(r["request_status"] == "CACHED" for r in raw),
            "timing": {"wq_seconds": sum(durations) if durations else None, "timed_requests": len(durations),
                       "recovered_requests": recovered,
                       "generation_seconds": sum(generations) if generations else None,
                       "qualification_seconds": sum(qualification_durations) if qualification_durations else None,
                       "pause_seconds": pause_seconds,
                       "recovery_seconds": sum(recovery_durations) if recovery_durations else None},
            "speed_claim": "NO_TIME_TO_PASS_ESTIMATE"}
