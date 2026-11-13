"""Deterministic DeepEval metrics backed by AIAC's own persisted boundaries."""
from __future__ import annotations

import json
from types import SimpleNamespace

from deepeval.metrics import BaseMetric

from backend.services.ppa_handoff import validate_boundary


def lifecycle_decision(data: dict) -> str:
    if (data.get("run_id") != data.get("latest_run_id")
            or data.get("run_status") != "PAUSED"
            or data.get("block_reason") not in {"PPA_FIRST_BATCH_REVIEW", "AWAITING_RESEARCH_BATCH"}):
        return "PPA_HANDOFF_RUN_STATE_INVALID"
    checkpoint = {"ppa_batch": {"cases": data.get("batch_cases") or []},
                  "ppa_batch_cursor": data.get("batch_cursor")}
    rows = [SimpleNamespace(
        experiment_group=row.get("case_id"),
        request_status=row.get("request_status"),
        result_snapshot={key: value for key, value in {
            "ppa_evaluation": ({"status": row["ppa_evaluation"]}
                               if row.get("ppa_evaluation") else None),
            "http_status": row.get("http_status"),
        }.items() if value is not None},
    ) for row in data.get("candidates") or []]
    try:
        validate_boundary(checkpoint, rows)
    except ValueError as exc:
        return str(exc)
    return "SAFE_BOUNDARY"


def quality_decision(data: dict) -> str:
    counts = data.get("quality_counts") or {}
    saved = data.get("saved_results")
    if (not isinstance(saved, int) or saved < 0
            or not isinstance(counts, dict)
            or any(not isinstance(n, int) or n < 0 for n in counts.values())
            or sum(counts.values()) != saved
            or data.get("reported_quality_pass") != counts.get("PASS", 0)):
        return "QUALITY_COUNT_MISMATCH"
    return "PERSISTED_PASS_ZERO" if counts.get("PASS", 0) == 0 else "PERSISTED_PASS_PRESENT"


def throughput_decision(data: dict) -> str:
    insufficient = ("INSUFFICIENT_EVIDENCE" if data.get("claim") == "INSUFFICIENT_EVIDENCE"
                    else "UNSUPPORTED_SPEED_CLAIM")
    before, after = data.get("before"), data.get("after")
    for window in (before, after):
        if not isinstance(window, dict):
            return insufficient
        if (not window.get("comparison_key") or not isinstance(window.get("completed_requests"), int)
                or window["completed_requests"] <= 0
                or not isinstance(window.get("wq_wall_seconds"), (int, float))
                or window["wq_wall_seconds"] <= 0
                or window.get("wq_wall_method") != "INTERVAL_UNION"
                or not isinstance(window.get("timed_requests"), int)
                or not isinstance(window.get("recovered_requests"), int)
                or window["timed_requests"] <= 0 or window["recovered_requests"] < 0
                or window["timed_requests"] + window["recovered_requests"] != window["completed_requests"]
                or not isinstance(window.get("actual_parent_peak"), int)
                or window["actual_parent_peak"] <= 0
                or window.get("pending_requests") != 0
                or not isinstance(window.get("pause_seconds_by_reason"), dict)
                or any(not isinstance(n, (int, float)) or n < 0
                       for n in window["pause_seconds_by_reason"].values())):
            return insufficient
    if before["comparison_key"] != after["comparison_key"]:
        return insufficient
    faster = (after["completed_requests"] / after["wq_wall_seconds"]
              > before["completed_requests"] / before["wq_wall_seconds"])
    if data.get("claim") == "FASTER" and faster:
        return "FASTER_EVIDENCED"
    if data.get("claim") == "NO_SPEEDUP" and not faster:
        return "NO_SPEEDUP_EVIDENCED"
    return "THROUGHPUT_CLAIM_MISMATCH"


class _OfflineMetric(BaseMetric):
    async_mode = False
    verbose_mode = False
    include_reason = True
    threshold = 1.0
    decision = None
    accepted: frozenset[str] = frozenset()

    def measure(self, test_case, *args, **kwargs) -> float:
        self.reason = self.decision(json.loads(test_case.actual_output))
        self.score = float(self.reason in self.accepted)
        self.success = bool(self.score)
        return self.score

    async def a_measure(self, test_case, *args, **kwargs) -> float:
        return self.measure(test_case)

    def is_successful(self) -> bool:
        return bool(self.success)

    @property
    def __name__(self):
        return self.__class__.__name__


class LifecycleMetric(_OfflineMetric):
    decision = staticmethod(lifecycle_decision)
    accepted = frozenset({"SAFE_BOUNDARY"})


class QualityMetric(_OfflineMetric):
    decision = staticmethod(quality_decision)
    accepted = frozenset({"PERSISTED_PASS_ZERO", "PERSISTED_PASS_PRESENT"})


class ThroughputMetric(_OfflineMetric):
    decision = staticmethod(throughput_decision)
    accepted = frozenset({"FASTER_EVIDENCED", "NO_SPEEDUP_EVIDENCED", "INSUFFICIENT_EVIDENCE"})
