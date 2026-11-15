"""Allowlisted research report snapshots and stable comparisons."""
from __future__ import annotations


def sanitize_snapshot(report: dict, candidates: list[dict]) -> dict:
    funnel = report.get("candidate_funnel") or {}
    progress = report.get("ppa_v2_progress") or {}
    timing = progress.get("timing") or {}
    return {
        "case_id": f"task{report['task_id']}-run{report['run_id']}",
        "source_observed_at": report.get("observed_at"),
        "source_commit": None,
        "agentsview_session_id": None,
        "task_id": report["task_id"],
        "run_id": report["run_id"],
        "run_status": report.get("status"),
        "batch_cursor": None,
        "generated": funnel.get("generated"),
        "saved_results": funnel.get("saved_wq_results"),
        "quality_pass": funnel.get("quality_pass"),
        "quality_counts": dict(report.get("quality_counts") or {}),
        "request_counts": dict(report.get("request_counts") or {}),
        "best_sharpe": (report.get("best_sharpe_alpha") or {}).get("sharpe"),
        "timing": {
            "completed_requests": funnel.get("wq_results"),
            "timed_requests": timing.get("timed_requests"),
            "recovered_requests": timing.get("recovered_requests"),
            "wq_wall_method": "INTERVAL_UNION",
            "scope": "CAMPAIGN_PROGRESS_WALL__RUN_RESULT_COUNT",
            "wq_wall_seconds": timing.get("wq_wall_seconds"),
            "actual_parent_peak": progress.get("actual_parent_peak"),
            "pause_seconds_by_reason": dict(timing.get("pause_seconds_by_reason") or {}),
        },
        "candidates": [{
            "candidate_id": row.get("id"),
            "case_id": row.get("experiment_group"),
            "request_status": row.get("request_status"),
            "quality_status": row.get("quality_status"),
        } for row in candidates],
    }


def compare_snapshot(saved: dict, current: dict) -> dict:
    """Ignore observation time; keep every substantive field in the diff."""
    return {key: {"saved": saved.get(key), "current": current.get(key)}
            for key in sorted(set(saved) | set(current))
            if key != "source_observed_at" and saved.get(key) != current.get(key)}
