"""Local-only coordinator for a continuous PPA run; never submits an Alpha."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import subprocess
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from backend.services.ppa_feedback import prepare_batch


class CoordinatorRetryExhausted(Exception):
    """A bounded retry cycle failed; keep the coordinator alive and alert."""


def request_json(url, payload=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=data, method="POST" if data is not None else "GET",
                  headers={"Content-Type": "application/json"} if data is not None else {})
    with urlopen(req, timeout=30) as response:
        return json.load(response)


def first_batch_reviewable(evidence, run_id):
    rows = [row for row in evidence["rows"] if row["run_id"] == run_id and row["source"] == "DISCOVERY"]
    first = rows[:4]
    return len(first) == 4 and all(
        row["request_status"] in {"RESULT", "CACHED"}
        and row["metrics"].get("amer_sharpe") is not None
        and row["platform_ppa_evaluation"].get("status") in {"PPA_REJECTED", "PPA_READY"}
        for row in first)


def acquire_lease(run_id, *, port=None):
    """A local socket prevents two coordinators from driving one run."""
    bind_port = 47000 + run_id if port is None else port
    if not 0 <= bind_port <= 65535:
        raise ValueError("PPA_COORDINATOR_PORT_INVALID")
    lease = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lease.bind(("127.0.0.1", bind_port))
    except BaseException:
        lease.close()
        raise
    return lease


def run_step_with_retry(operation, output_dir, *, sleep=time.sleep):
    output_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    while True:
        try:
            action = operation()
            state = {"at": datetime.now(timezone.utc).isoformat(), "action": action,
                     "consecutive_failures": 0}
            (output_dir / "coordinator-heartbeat.json").write_text(json.dumps(state), encoding="utf-8")
            return action
        except (ValueError, OSError, TimeoutError, subprocess.CalledProcessError, URLError) as exc:
            recoverable = (not isinstance(exc, ValueError)
                           or str(exc).startswith(("COCKPIT_BATCH_INVALID", "PPA_BATCH_PHASE_LIMIT")))
            if not recoverable:
                raise
            failures += 1
            wait = (10, 30, 60, 300)[min(failures - 1, 3)]
            state = {"at": datetime.now(timezone.utc).isoformat(), "action": "RETRY",
                     "consecutive_failures": failures, "error": type(exc).__name__,
                     "retry_seconds": wait, "attention_required": failures >= 4}
            (output_dir / "coordinator-heartbeat.json").write_text(json.dumps(state), encoding="utf-8")
            print(json.dumps(state), flush=True)
            if failures >= 4:
                (output_dir / "coordinator-alert.json").write_text(json.dumps(state), encoding="utf-8")
                raise CoordinatorRetryExhausted from exc
            sleep(wait)


def step(api, task_id, run_id, cockpit_script, output_dir):
    task = request_json(f"{api}/tasks/{task_id}")
    runs = request_json(f"{api}/tasks/{task_id}/runs")
    run = next((row for row in runs if row["id"] == run_id), None)
    if run is None:
        raise ValueError("PPA_RUN_MISSING")
    if task["status"] in {"COMPLETED", "STOPPED", "FAILED"}:
        return "STOP"
    if task["status"] == "RUNNING":
        return "WAIT"
    if task["status"] != "PAUSED" or run["status"] != "PAUSED":
        raise ValueError("PPA_RUN_STATE_UNEXPECTED")
    reason = run.get("block_reason")
    if reason == "PPA_FIRST_BATCH_REVIEW":
        evidence = request_json(f"{api}/runs/{run_id}/ppa-feedback")
        if evidence.get("unresolved_requests"):
            recovered = request_json(f"{api}/runs/{run_id}/ppa-reconcile", {})
            if recovered["pending"]:
                return "WAIT_RECONCILE"
            evidence = request_json(f"{api}/runs/{run_id}/ppa-feedback")
        if not first_batch_reviewable(evidence, run_id):
            raise ValueError("PPA_FIRST_BATCH_EVIDENCE_INCOMPLETE")
        request_json(f"{api}/tasks/{task_id}/intervene", {"action": "RESUME"})
        return "RESUMED_REVIEWED"
    if reason == "PPA_BATCH_READY":
        request_json(f"{api}/tasks/{task_id}/intervene", {"action": "RESUME"})
        return "RESUMED_BATCH"
    if reason == "PPA_REQUEST_RECONCILIATION_PENDING":
        recovered = request_json(f"{api}/runs/{run_id}/ppa-reconcile", {})
        return "WAIT_RECONCILE" if recovered["pending"] else "RECONCILED"
    if reason == "PPA_RATE_LIMIT":
        retry_at = run.get("next_retry_at")
        if not retry_at:
            raise ValueError("PPA_RATE_LIMIT_RESET_UNKNOWN")
        reset_time = datetime.fromisoformat(retry_at)
        if reset_time.tzinfo is None:
            reset_time = reset_time.replace(tzinfo=timezone.utc)
        if reset_time > datetime.now(timezone.utc):
            return "WAIT_RATE_LIMIT"
        request_json(f"{api}/tasks/{task_id}/intervene", {"action": "RESUME"})
        return "RESUMED_RATE_LIMIT"
    if reason != "AWAITING_RESEARCH_BATCH":
        raise ValueError(f"PPA_REQUIRES_REVIEW:{reason}")
    evidence = request_json(f"{api}/runs/{run_id}/ppa-feedback")
    if evidence.get("unresolved_requests"):
        recovered = request_json(f"{api}/runs/{run_id}/ppa-reconcile", {})
        return "WAIT_RECONCILE" if recovered["pending"] else "RECONCILED"
    if evidence["next_phase"] is None:
        if not evidence.get("continuous"):
            raise ValueError(f"PPA_NO_NEXT_PHASE:{evidence['stop_reason']}")
        request_json(f"{api}/runs/{run_id}/ppa-epoch", {"expected_epoch": evidence["epoch"]})
        evidence = request_json(f"{api}/runs/{run_id}/ppa-feedback")
        if evidence["next_phase"] is None:
            raise ValueError(f"PPA_NO_NEXT_PHASE:{evidence['stop_reason']}")
    batch = prepare_batch(evidence["base"], evidence, cockpit_script)
    output_dir.mkdir(parents=True, exist_ok=True)
    batch_path = output_dir / f"run-{run_id}-epoch-{evidence['epoch']}-{batch['phase']}-{batch['candidate_generation']['plan_sha256'][:12]}.json"
    batch_path.write_text(json.dumps(batch, indent=2, ensure_ascii=False), encoding="utf-8")
    request_json(f"{api}/runs/{run_id}/ppa-batch", batch)
    request_json(f"{api}/tasks/{task_id}/intervene", {"action": "RESUME"})
    return f"IMPORTED:{batch_path.name}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", type=int, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--cockpit-script", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--api-base", default="http://127.0.0.1:8003/api/v1")
    args = parser.parse_args()
    if args.api_base not in {"http://127.0.0.1:8003/api/v1", "http://localhost:8003/api/v1"}:
        raise ValueError("Only the local AIAC API is allowed")
    lease = acquire_lease(args.run_id)
    try:
        while True:
            try:
                action = run_step_with_retry(
                    lambda: step(args.api_base, args.task_id, args.run_id, args.cockpit_script, args.output_dir),
                    args.output_dir)
            except CoordinatorRetryExhausted:
                time.sleep(300)
                continue
            print(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "action": action}), flush=True)
            if action == "STOP":
                break
            time.sleep(60 if action in {"WAIT_RECONCILE", "WAIT_RATE_LIMIT"} else 10)
    finally:
        lease.close()


if __name__ == "__main__":
    main()
