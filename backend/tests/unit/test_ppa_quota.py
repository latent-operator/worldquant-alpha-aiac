from backend.services.ppa_quota import QuotaWindow
from backend.services.ppa_quota import choose_parallelism
from backend.services.ppa_quota import restore_quota
from backend.services.ppa_quota import next_transport
from backend.services import ppa_quota
from datetime import datetime, timezone, timedelta


def test_quota_window_stops_after_observed_remaining_is_claimed():
    quota = QuotaWindow()
    assert quota.claim()
    quota.observe({"limit": 500, "remaining": 2, "reset_seconds": 3600})
    assert quota.claim()
    assert quota.claim()
    assert not quota.claim()
    quota.observe({"remaining": 0, "reset_seconds": 3600})
    assert not quota.claim()


def test_quota_window_never_increases_balance_on_stale_response():
    quota = QuotaWindow()
    quota.observe({"remaining": 1, "reset_seconds": 60})
    assert quota.claim()
    quota.observe({"remaining": 3, "reset_seconds": 60})
    assert not quota.claim()


def test_parallelism_ramps_only_after_measured_batch_and_selects_best():
    two = {"concurrency": 2, "completed": 24, "wall_seconds": 2400,
           "unknown": 0, "rate_limited": False}
    four = {"concurrency": 4, "completed": 24, "wall_seconds": 1800,
            "unknown": 0, "rate_limited": False}
    eight = {"concurrency": 8, "completed": 24, "wall_seconds": 1900,
             "unknown": 0, "rate_limited": False}
    assert choose_parallelism(2, [two]) == 4
    assert choose_parallelism(4, [two, four]) == 8
    assert choose_parallelism(8, [two, four, eight]) == 4
    assert choose_parallelism(4, [two, {**four, "unknown": 1}]) == 2


def test_four_case_batches_accumulate_evidence_for_each_ramp():
    two = {"transport": "single", "concurrency": 2, "completed": 4,
           "wall_seconds": 350, "unknown": 0, "rate_limited": False}
    four = {"transport": "single", "concurrency": 4, "completed": 4,
            "wall_seconds": 300, "unknown": 0, "rate_limited": False}
    assert choose_parallelism(2, [two] * 5) == 2
    assert choose_parallelism(2, [two] * 6) == 4
    assert choose_parallelism(4, [two] * 6 + [four] * 5) == 4
    assert choose_parallelism(4, [two] * 6 + [four] * 6) == 8


def test_parallelism_requires_both_twenty_four_results_and_thirty_active_minutes():
    assert choose_parallelism(2, [{"concurrency": 2, "completed": 24,
                                   "wall_seconds": 600}]) == 2
    assert choose_parallelism(2, [{"concurrency": 2, "completed": 20,
                                   "wall_seconds": 1800}]) == 2


def test_experiment_does_not_demote_or_switch_transport_for_underfilled_batch():
    supply_limited = {"transport": "single", "concurrency": 4, "parent_peak": 2,
                      "parent_groups": 2, "supply_limited": True, "completed": 2,
                      "wall_seconds": 300, "unknown": 0, "rate_limited": False}
    assert choose_parallelism(4, [supply_limited]) == 4
    assert next_transport("single", 4, [supply_limited]) == ("single", 4)
    platform_limited = {**supply_limited, "parent_groups": 8, "supply_limited": False}
    assert choose_parallelism(4, [platform_limited]) == 2


def test_underfilled_batch_without_experiment_flag_is_not_platform_failure():
    row = {"transport": "single", "concurrency": 8, "parent_peak": 4,
           "parent_groups": 4, "supply_limited": False, "completed": 4,
           "wall_seconds": 300, "unknown": 0, "rate_limited": False}
    assert choose_parallelism(8, [row]) == 8
    assert next_transport("single", 8, [row]) == ("single", 8)
    pair_row = {**row, "transport": "paired_multi", "parent_peak": 2,
                "parent_groups": 2}
    assert next_transport("paired_multi", 8, [pair_row]) == ("paired_multi", 8)


def test_previous_concurrency_peak_cannot_undo_new_level_before_first_batch():
    prior = {"transport": "single", "concurrency": 2, "parent_peak": 2,
             "parent_groups": 5, "supply_limited": False, "completed": 5,
             "wall_seconds": 634.468, "unknown": 0, "rate_limited": False}
    assert choose_parallelism(4, [prior]) == 4
    assert next_transport("single", 4, [prior]) == ("single", 4)


def test_quota_restores_only_unexpired_observation():
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    recent = {"request": {"rate_limit": {"remaining": 3, "reset_seconds": 3600},
                          "rate_limit_observed_at": (now - timedelta(minutes=10)).isoformat()}}
    stale = {"request": {"rate_limit": {"remaining": 0, "reset_seconds": 60},
                         "rate_limit_observed_at": (now - timedelta(minutes=20)).isoformat()}}
    quota = restore_quota([stale, recent], now)
    assert quota.remaining == 3
    assert quota.claim()


def test_pair_claim_requires_two_child_slots():
    quota = QuotaWindow()
    quota.observe({"remaining": 1, "reset_seconds": 60})
    assert not quota.claim_many(2)
    assert quota.remaining == 1
    assert quota.claim_many(1)


def test_429_with_remaining_balance_uses_short_cooldown_not_daily_reset():
    quota = QuotaWindow()
    quota.observe({"limit": 5000, "remaining": 4900, "reset_seconds": 60000})
    assert ppa_quota.retry_delay_for_429(quota, retry_after=None, streak=1) == 30
    assert ppa_quota.retry_delay_for_429(quota, retry_after=75, streak=2) == 75
    assert ppa_quota.retry_delay_for_429(quota, retry_after=None, streak=6) == 600


def test_429_with_zero_remaining_waits_for_reset():
    quota = QuotaWindow()
    quota.observe({"limit": 5000, "remaining": 0, "reset_seconds": 3600})
    assert 3590 <= ppa_quota.retry_delay_for_429(quota, retry_after=30, streak=1) <= 3600.01


def test_429_does_not_treat_local_reservations_as_server_daily_exhaustion():
    quota = QuotaWindow()
    quota.observe({"limit": 5000, "remaining": 2, "reset_seconds": 3600})
    assert quota.claim_many(2)
    assert quota.remaining == 0
    assert ppa_quota.retry_delay_for_429(quota, retry_after=30, streak=1) == 30


def test_stalled_single_requests_try_audited_pairs_at_next_batch_boundary():
    history = [{"transport": "single", "concurrency": 2, "completed": 24,
                "wall_seconds": 1800, "unknown": 0, "rate_limited": False},
               {"transport": "single", "concurrency": 4, "completed": 24,
                "wall_seconds": 1800, "unknown": 0, "rate_limited": False}]
    assert next_transport("single", 4, history) == ("paired_multi", 2)


def test_underfilled_eight_way_pair_batch_recalibrates_with_singles():
    old = {"transport": "paired_multi", "concurrency": 8,
           "completed": 16, "wall_seconds": 300, "unknown": 0, "rate_limited": False}
    new = {**old, "completed": 4, "parent_peak": 2}
    assert next_transport("paired_multi", 8, [old, new]) == ("single", 2)
