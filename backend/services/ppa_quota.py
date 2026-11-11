"""Conservative per-process view of WQ's observed daily simulation allowance."""
import time
from datetime import datetime, timezone, timedelta


def next_parallelism(current, history):
    """Increase only after two complete, full, clean batches at the current level."""
    current = min(4, max(2, int(current)))
    recent = history[-2:]
    if (current == 2 and len(recent) == 2 and all(item.get("batch_hash") for item in recent)
            and recent[0]["batch_hash"] != recent[1]["batch_hash"] and all(
        item.get("concurrency") == 2 and item.get("cases", 0) >= 2
        and item.get("completed") == item.get("cases")
        and item.get("parent_peak", 0) >= 2 and not item.get("supply_limited")
        and not item.get("rate_limited") and not item.get("unknown")
        and not item.get("execution_error") for item in recent
    )):
        return 4
    return current


class QuotaWindow:
    def __init__(self):
        self.limit = None
        self.remaining = None
        self.observed_remaining = None
        self.reset_at = None

    def observe(self, values):
        if not isinstance(values, dict):
            return
        now = time.monotonic()
        if self.reset_at is not None and now >= self.reset_at:
            self.remaining = None
            self.observed_remaining = None
            self.reset_at = None
        limit = values.get("limit")
        remaining = values.get("remaining")
        reset = values.get("reset_seconds")
        if type(limit) is int and limit >= 0:
            self.limit = limit
        if type(reset) is int and reset >= 0:
            self.reset_at = now + reset if self.reset_at is None else min(self.reset_at, now + reset)
        if type(remaining) is int and remaining >= 0:
            self.observed_remaining = (remaining if self.observed_remaining is None
                                       else min(self.observed_remaining, remaining))
            self.remaining = remaining if self.remaining is None else min(self.remaining, remaining)

    def claim(self):
        return self.claim_many(1)

    def claim_many(self, count):
        if type(count) is not int or count < 1:
            raise ValueError("PPA_QUOTA_CLAIM_INVALID")
        if self.reset_at is not None and time.monotonic() >= self.reset_at:
            self.remaining = None
            self.observed_remaining = None
            self.reset_at = None
        if self.remaining is not None:
            if self.remaining < count:
                return False
            self.remaining -= count
        return True


def retry_delay_for_429(quota, retry_after=None, streak=1):
    """Distinguish a burst limit from an observed exhausted daily allowance."""
    try:
        server_wait = max(0.0, float(retry_after))
    except (TypeError, ValueError):
        server_wait = 0.0
    if quota is not None and quota.observed_remaining == 0 and quota.reset_at is not None:
        return max(server_wait, max(0.0, quota.reset_at - time.monotonic()))
    return max(server_wait, min(600, 30 * (2 ** min(max(0, streak - 1), 5))))


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
