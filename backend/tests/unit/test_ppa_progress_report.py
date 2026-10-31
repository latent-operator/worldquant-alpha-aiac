from backend.services import ppa_progress as progress
from datetime import datetime, timezone, timedelta


def test_report_excludes_dedup_and_keeps_denominator_and_amer():
    rows = [dict(id=1, request_status="RESULT", source="DISCOVERY", arm="CONTROL", pair_id="p",
                 run_id=1, metrics={"sharpe": 1, "fitness": 0.5, "amer_sharpe": 0.7, "test_sharpe": 0.4}),
            dict(id=2, request_status="RESULT", source="REFINEMENT", parent_candidate_id=1, run_id=2,
                 metrics={"sharpe": 1.1, "fitness": 0.6, "amer_sharpe": 0.9, "test_sharpe": 0.5,
                          "turnover": 0.2, "margin": 0.001, "long_count": 100, "short_count": 100}),
            dict(id=3, request_status="NOT_REQUESTED", source="REFINEMENT", run_id=2, metrics={})]
    report = progress.progress_report(rows, current_run_id=2)
    assert report["new_requests"] == 1
    assert report["effective_improvements"] == 1
    assert report["per_ten_requests"] is None
    assert round(report["parent_deltas"][0]["amer_sharpe"], 2) == 0.2
    assert report["timing"]["wq_seconds"] is None


def test_empty_report_does_not_claim_zero_correlation_or_pass():
    report = progress.progress_report([], 2)
    assert report["best_amer_positive_test"] is None
    assert report["ppa_ready"] == 0


def test_timing_separates_direct_wq_recovery_generation_and_pause():
    rows = [dict(id=1, run_id=2, request_status="RESULT", source="DISCOVERY",
                 metrics={"sharpe": 0.3, "fitness": 0.1, "amer_sharpe": 0.2,
                          "test_sharpe": 0.1},
                 result_snapshot={"request": {"sent_at": "2026-10-30T12:00:00+00:00",
                                              "finished_at": "2026-10-30T12:01:00+00:00"},
                                  "qualification_seconds": 3}),
            dict(id=2, run_id=2, request_status="RESULT", source="DISCOVERY",
                 metrics={"sharpe": 0.2, "fitness": 0.1, "amer_sharpe": 0.1,
                          "test_sharpe": 0.1},
                 result_snapshot={"request": {"sent_at": "2026-10-30T11:00:00+00:00",
                                              "finished_at": "2026-10-30T12:00:00+00:00"},
                                  "recovery": {"source": "original_location", "duration_seconds": 20},
                                  "qualification_seconds": 2})]
    checkpoint = {"ppa_pause_seconds": 90,
                  "ppa_batch_history": [{"candidate_generation": {"generation_seconds": 7}}],
                  "ppa_batch": {"candidate_generation": {"generation_seconds": 5}}}
    report = progress.progress_report(rows, current_run_id=2, checkpoint=checkpoint)
    assert report["timing"] == {"wq_seconds": 60, "wq_wall_seconds": 60, "timed_requests": 1,
                                "recovered_requests": 1, "generation_seconds": 12,
                                "qualification_seconds": 5, "pause_seconds": 90,
                                "pause_seconds_by_reason": {},
                                "recovery_seconds": 20}


def test_pause_reason_is_recorded_once_on_resume():
    start = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
    checkpoint = progress.begin_pause({}, "AWAITING_RESEARCH_BATCH", start)
    checkpoint = progress.begin_pause(checkpoint, "AWAITING_RESEARCH_BATCH", start + timedelta(seconds=5))
    checkpoint = progress.close_pause(checkpoint, start + timedelta(seconds=20))
    checkpoint = progress.close_pause(checkpoint, start + timedelta(seconds=30))
    assert checkpoint["ppa_pause_by_reason"] == {"AWAITING_RESEARCH_BATCH": 20}
    assert checkpoint["ppa_pause_seconds"] == 20


def test_parallel_wq_wall_time_uses_union_not_sum():
    rows = [dict(id=index, run_id=2, request_status="RESULT", source="DISCOVERY", metrics={},
                 result_snapshot={"request": {"sent_at": sent, "finished_at": done}})
            for index, sent, done in ((1, "2026-10-30T12:00:00+00:00", "2026-10-30T12:01:00+00:00"),
                                      (2, "2026-10-30T12:00:30+00:00", "2026-10-30T12:01:30+00:00"))]
    report = progress.progress_report(rows, current_run_id=2)
    assert report["timing"]["wq_seconds"] == 120
    assert report["timing"]["wq_wall_seconds"] == 90


def test_report_exposes_observed_quota_and_batch_throughput():
    rows = [dict(id=1, run_id=2, request_status="RESULT", source="DISCOVERY", metrics={},
                 result_snapshot={"request": {"rate_limit": {"limit": 500, "remaining": 17,
                                                               "reset_seconds": 3600},
                                              "rate_limit_observed_at": "2026-10-31T12:00:00+00:00"}})]
    checkpoint = {"ppa_max_in_flight": 4, "ppa_throughput_history": [
        {"concurrency": 2, "completed": 12, "wall_seconds": 120, "unknown": 0,
         "rate_limited": False}]}
    report = progress.progress_report(rows, current_run_id=2, checkpoint=checkpoint)
    assert report["observed_quota"]["remaining"] == 17
    assert report["parallelism"] == 4
    assert report["batch_throughput"][0]["completed"] == 12
