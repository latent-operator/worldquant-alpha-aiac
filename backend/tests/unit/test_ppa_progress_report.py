from backend.services import ppa_progress as progress


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
    assert report["timing"] == {"wq_seconds": 60, "timed_requests": 1,
                                "recovered_requests": 1, "generation_seconds": 12,
                                "qualification_seconds": 5, "pause_seconds": 90,
                                "recovery_seconds": 20}
