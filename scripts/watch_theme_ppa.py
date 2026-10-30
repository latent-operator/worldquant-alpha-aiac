"""Local-only coordinator for a continuous PPA run; never submits an Alpha."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen

from backend.services.ppa_feedback import prepare_batch


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
        if not first_batch_reviewable(evidence, run_id):
            raise ValueError("PPA_FIRST_BATCH_EVIDENCE_INCOMPLETE")
        request_json(f"{api}/tasks/{task_id}/intervene", {"action": "RESUME"})
        return "RESUMED_REVIEWED"
    if reason == "PPA_BATCH_READY":
        request_json(f"{api}/tasks/{task_id}/intervene", {"action": "RESUME"})
        return "RESUMED_BATCH"
    if reason != "AWAITING_RESEARCH_BATCH":
        raise ValueError(f"PPA_REQUIRES_REVIEW:{reason}")
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
    while True:
        action = step(args.api_base, args.task_id, args.run_id, args.cockpit_script, args.output_dir)
        print(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "action": action}), flush=True)
        if action == "STOP":
            break
        time.sleep(10)


if __name__ == "__main__":
    main()
