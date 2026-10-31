"""Conservative per-process view of WQ's observed daily simulation allowance."""
import time
from datetime import datetime, timezone, timedelta


class QuotaWindow:
    def __init__(self):
        self.limit = None
        self.remaining = None
        self.reset_at = None

    def observe(self, values):
        if not isinstance(values, dict):
            return
        now = time.monotonic()
        if self.reset_at is not None and now >= self.reset_at:
            self.remaining = None
            self.reset_at = None
        limit = values.get("limit")
        remaining = values.get("remaining")
        reset = values.get("reset_seconds")
        if type(limit) is int and limit >= 0:
            self.limit = limit
        if type(reset) is int and reset >= 0:
            self.reset_at = now + reset if self.reset_at is None else min(self.reset_at, now + reset)
        if type(remaining) is int and remaining >= 0:
            self.remaining = remaining if self.remaining is None else min(self.remaining, remaining)

    def claim(self):
        return self.claim_many(1)

    def claim_many(self, count):
        if type(count) is not int or count < 1:
            raise ValueError("PPA_QUOTA_CLAIM_INVALID")
        if self.reset_at is not None and time.monotonic() >= self.reset_at:
            self.remaining = None
            self.reset_at = None
        if self.remaining is not None:
            if self.remaining < count:
                return False
            self.remaining -= count
        return True


def choose_parallelism(current, history, transport="single"):
    """Use real completed batches, never the theoretical eight-job ceiling."""
    current = current if current in (2, 4, 8) else 2
    history = [row for row in history if row.get("transport", "single") == transport]
    if history and history[-1].get("concurrency") == current and (
            history[-1].get("unknown", 0) or history[-1].get("rate_limited")):
        return max(2, current // 2)
    totals = {}
    for row in history:
        level, count, seconds = row.get("concurrency"), row.get("completed", 0), row.get("wall_seconds", 0)
        if (level in (2, 4, 8) and count > 0 and seconds > 0
                and not row.get("unknown") and not row.get("rate_limited")):
            prior_count, prior_seconds = totals.get(level, (0, 0))
            totals[level] = prior_count + count, prior_seconds + seconds
    measured = {level: count / seconds for level, (count, seconds) in totals.items()
                if count >= 8}
    if current == 2 and 2 in measured and 4 not in measured:
        return 4
    if current == 4 and 4 in measured and 8 not in measured:
        if 2 not in measured or measured[4] >= 1.1 * measured[2]:
            return 8
        return 2
    if 8 in measured:
        return max(measured, key=measured.get)
    return current


def next_transport(transport, current, history):
    level = choose_parallelism(current, history, transport)
    latest = history[-1] if history else {}
    if (transport == "single" and current >= 4 and level < current
            and not latest.get("unknown") and not latest.get("rate_limited")):
        return "paired_multi", 2
    return transport, level


def restore_quota(snapshots, now=None):
    now = now or datetime.now(timezone.utc)
    quota = QuotaWindow()
    observations = []
    for snapshot in snapshots:
        request = (snapshot or {}).get("request") or {}
        values = request.get("rate_limit") or {}
        try:
            observed = datetime.fromisoformat(request["rate_limit_observed_at"])
            reset = values["reset_seconds"]
            if observed.tzinfo is None or type(reset) is not int or reset < 0:
                continue
            expiry = observed + timedelta(seconds=reset)
            if expiry > now:
                observations.append((observed, values, expiry))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    if observations:
        _, values, expiry = max(observations, key=lambda item: item[0])
        quota.observe({**values, "reset_seconds": max(1, int((expiry - now).total_seconds()))})
    return quota
