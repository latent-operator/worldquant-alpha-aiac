"""Auditable progress, never a prediction of time-to-PASS."""
from collections import Counter
from datetime import datetime, timezone
from backend.services.ppa_feedback import research_row
from backend.services.ppa_research_policy import evidence_metrics, effective_improvement


def begin_pause(checkpoint, reason, now=None):
    now = now or datetime.now(timezone.utc)
    updated = dict(checkpoint or {})
    if updated.get("ppa_pause_started_at"):
        if updated.get("ppa_pause_reason") == reason:
            return updated
        updated = close_pause(updated, now)
    updated["ppa_pause_started_at"] = now.isoformat()
    updated["ppa_pause_reason"] = reason
    return updated


def close_pause(checkpoint, now=None):
    now = now or datetime.now(timezone.utc)
    updated = dict(checkpoint or {})
    began = updated.pop("ppa_pause_started_at", None)
    reason = updated.pop("ppa_pause_reason", "UNATTRIBUTED")
    if began:
        try:
            seconds = max(0, (now - datetime.fromisoformat(began)).total_seconds())
            updated["ppa_pause_seconds"] = updated.get("ppa_pause_seconds", 0) + seconds
            by_reason = dict(updated.get("ppa_pause_by_reason") or {})
            by_reason[reason] = by_reason.get(reason, 0) + seconds
            updated["ppa_pause_by_reason"] = by_reason
        except (TypeError, ValueError):
            pass
    return updated


def interval_union_seconds(intervals):
    total = 0
    end = None
    for start, finish in sorted(intervals):
        if end is None or start > end:
            total += (finish - start).total_seconds()
            end = finish
        elif finish > end:
            total += (finish - end).total_seconds()
            end = finish
    return total


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
    durations, intervals, recovered, recovery_durations, qualification_durations = [], [], 0, [], []
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
                start, finish = datetime.fromisoformat(request["sent_at"]), datetime.fromisoformat(request["finished_at"])
                durations.append((finish - start).total_seconds())
                intervals.append((start, finish))
        except (KeyError, TypeError, ValueError):
            pass
    generations = [batch.get("candidate_generation", {}).get("generation_seconds") for batch in
                   [*(checkpoint.get("ppa_batch_history") or []), checkpoint.get("ppa_batch") or {}]]
    generations = [value for value in generations if isinstance(value, (int, float))]
    quota_observations = [((row.get("result_snapshot") or {}).get("request") or {}) for row in raw]
    quota_observations = [request for request in quota_observations if request.get("rate_limit_observed_at")
                          and isinstance(request.get("rate_limit"), dict)]
    latest_quota = (max(quota_observations, key=lambda request: request["rate_limit_observed_at"])
                    if quota_observations else None)
    pause_seconds = checkpoint.get("ppa_pause_seconds")
    pause_by_reason = dict(checkpoint.get("ppa_pause_by_reason") or {})
    if checkpoint.get("ppa_pause_started_at"):
        try:
            active = (datetime.now(timezone.utc) - datetime.fromisoformat(
                checkpoint["ppa_pause_started_at"])).total_seconds()
            pause_seconds = (pause_seconds or 0) + max(0, active)
            reason = checkpoint.get("ppa_pause_reason") or "UNATTRIBUTED"
            pause_by_reason[reason] = pause_by_reason.get(reason, 0) + max(0, active)
        except (TypeError, ValueError):
            pass
    return {"new_requests": len(new), "effective_improvements": gains,
            "per_ten_requests": gains / len(new) * 10 if len(new) >= 10 else None,
            "parent_deltas": deltas, "best_amer_positive_test": {"candidate_id": best["id"],
                "amer_sharpe": best["metrics"]["amer_sharpe"],
                "gap": max(0, 1 - best["metrics"]["amer_sharpe"])} if best else None,
            "ppa_ready": sum(row["ppa_evaluation"].get("status") == "PPA_READY" for row in measured),
            "evidence_reasons": dict(missing), "cached_results": sum(r["request_status"] == "CACHED" for r in raw),
            "observed_quota": ({**latest_quota["rate_limit"],
                                "observed_at": latest_quota["rate_limit_observed_at"]}
                               if latest_quota else None),
            "parallelism": checkpoint.get("ppa_max_in_flight", 1),
            "batch_throughput": (checkpoint.get("ppa_throughput_history") or [])[-20:],
            "timing": {"wq_seconds": sum(durations) if durations else None,
                       "wq_wall_seconds": interval_union_seconds(intervals) if intervals else None,
                       "timed_requests": len(durations),
                       "recovered_requests": recovered,
                       "generation_seconds": sum(generations) if generations else None,
                       "qualification_seconds": sum(qualification_durations) if qualification_durations else None,
                       "pause_seconds": pause_seconds,
                       "pause_seconds_by_reason": pause_by_reason,
                       "recovery_seconds": sum(recovery_durations) if recovery_durations else None},
            "speed_claim": "NO_TIME_TO_PASS_ESTIMATE"}
