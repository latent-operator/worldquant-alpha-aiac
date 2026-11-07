from backend.services.ppa_quota import QuotaWindow
from backend.services.ppa_quota import restore_quota
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
