from backend.services.ppa_quota import QuotaWindow
from backend.services.ppa_quota import choose_parallelism
from backend.services.ppa_quota import restore_quota
from backend.services.ppa_quota import next_transport
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
    two = {"concurrency": 2, "completed": 12, "wall_seconds": 120,
           "unknown": 0, "rate_limited": False}
    four = {"concurrency": 4, "completed": 12, "wall_seconds": 70,
            "unknown": 0, "rate_limited": False}
    eight = {"concurrency": 8, "completed": 12, "wall_seconds": 75,
             "unknown": 0, "rate_limited": False}
    assert choose_parallelism(2, [two]) == 4
    assert choose_parallelism(4, [two, four]) == 8
    assert choose_parallelism(8, [two, four, eight]) == 4
    assert choose_parallelism(4, [two, {**four, "unknown": 1}]) == 2


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


def test_stalled_single_requests_try_audited_pairs_at_next_batch_boundary():
    history = [{"transport": "single", "concurrency": 2, "completed": 16,
                "wall_seconds": 100, "unknown": 0, "rate_limited": False},
               {"transport": "single", "concurrency": 4, "completed": 16,
                "wall_seconds": 100, "unknown": 0, "rate_limited": False}]
    assert next_transport("single", 4, history) == ("paired_multi", 2)
