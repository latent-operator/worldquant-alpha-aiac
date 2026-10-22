"""Wait for the sole Celery worker to finish its current round before release."""

from __future__ import annotations

import argparse
import time


def is_idle(active: dict | None) -> bool:
    return bool(active) and all(not tasks for tasks in active.values())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    args = parser.parse_args()
    from backend.celery_app import celery_app

    deadline = time.monotonic() + args.timeout_seconds
    while time.monotonic() < deadline:
        try:
            active = celery_app.control.inspect(timeout=5).active()
            if is_idle(active):
                print("AIAC worker is idle.")
                return 0
        except Exception:
            pass
        time.sleep(5)
    print("AIAC worker did not confirm an idle round before the timeout.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
