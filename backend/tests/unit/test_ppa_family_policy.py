from backend.services import ppa_research_policy as policy


def test_daily_correlation_aligns_dates_and_requires_enough_history():
    a = {f"2026-01-{i:02}": float(i * i) for i in range(1, 22)}
    b = {day: value * 2 for day, value in a.items()}
    assert policy.daily_correlation(a, b, min_samples=20)["correlation"] == 1.0
    assert policy.daily_correlation(a, {}, min_samples=20)["correlation"] is None


def test_gap_is_not_treated_as_a_daily_return():
    a = {"2026-01-01": 1, "2026-01-02": 3, "2026-01-03": 5}
    b = {"2026-01-01": 1, "2026-01-03": 5}
    assert policy.daily_correlation(a, b, min_samples=2)["samples"] == 0
