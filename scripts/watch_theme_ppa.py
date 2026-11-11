"""Local-only coordinator for a continuous PPA run; never submits an Alpha."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import subprocess
import time
from types import SimpleNamespace
from urllib.error import URLError
from urllib.request import Request, urlopen

from backend.services.ppa_feedback import (prepare_batch_filled as prepare_batch, freeze_batch,
                                           freeze_seed_expansion, freeze_measured_seed_expansion,
                                           freeze_measured_discovery_expansion)


def prepare_with_policy(base, evidence, cockpit_script):
    if evidence.get("local_expansion"):
        if evidence.get("next_phase") == "discovery":
            try:
                return freeze_measured_discovery_expansion(base, evidence)
            except ValueError as exc:
                if str(exc) != "PPA_NO_LOCAL_VARIANTS":
                    raise
        if evidence.get("next_phase") == "refinement":
            try:
                return freeze_measured_seed_expansion(base, evidence)
            except ValueError as exc:
                if str(exc) != "PPA_NO_LOCAL_VARIANTS":
                    raise
        return prepare_batch(base, evidence, cockpit_script, local_expansion=True)
    return prepare_batch(base, evidence, cockpit_script)


class CoordinatorRetryExhausted(Exception):
    """A bounded retry cycle failed; keep the coordinator alive and alert."""


def model_account_unavailable(exc):
    """Identify Cockpit's quota or account routing failure without logging stderr."""
    if not isinstance(exc, subprocess.CalledProcessError):
        return False
    stderr = exc.stderr
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    return "no compatible account is available for this model" in str(stderr or "").lower()


def request_json(url, payload=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=data, method="POST" if data is not None else "GET",
                  headers={"Content-Type": "application/json"} if data is not None else {})
    with urlopen(req, timeout=30) as response:
        return json.load(response)


def first_batch_reviewable(evidence, run_id, cases):
    if type(evidence.get("unresolved_requests")) is not int or evidence["unresolved_requests"] != 0:
        return False
    allocation = (evidence.get("base") or {}).get("discovery_allocation")
    if allocation:
        if not all(isinstance(case, dict) for case in cases):
            return False
        groups = {}
        for case in cases:
            pair, mechanism = case.get("pair_id"), case.get("mechanism_id")
            if not pair or not mechanism or (pair in groups and groups[pair][0] != mechanism):
                return False
            groups.setdefault(pair, (mechanism, []))[1].append(case)
        if (dict(Counter(value[0] for value in groups.values())) != allocation or
                any(len(group) != 4 or Counter(c.get("arm") for c in group) !=
                    {"CONTROL": 1, "TREATMENT": 3} for _, group in groups.values())):
            return False
    case_ids = [case.get("case_id") if isinstance(case, dict) else case for case in cases]
    if not all(case_ids) or len(set(case_ids)) != len(case_ids):
        return False
    rows = {row["case_id"]: row for row in evidence["rows"]
            if row["run_id"] == run_id and row["source"] == "DISCOVERY"}
    return bool(case_ids) and all(
        case_id in rows and rows[case_id]["request_status"] in {"RESULT", "CACHED"}
        and rows[case_id]["metrics"].get("amer_sharpe") is not None
        and rows[case_id]["platform_ppa_evaluation"].get("status") in {"PPA_REJECTED", "PPA_READY"}
        for case_id in case_ids)


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


def run_step_with_retry(operation, output_dir, *, sleep=time.sleep, enabled=lambda: True):
    output_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    model_waits = 0
    while True:
        if not enabled():
            return "STOP"
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
            if model_account_unavailable(exc):
                model_waits += 1
                state = {"at": datetime.now(timezone.utc).isoformat(),
                         "action": "WAIT_MODEL_ACCOUNT", "consecutive_failures": model_waits,
                         "error": "MODEL_UNAVAILABLE", "retry_seconds": 300,
                         "attention_required": model_waits >= 72}
                (output_dir / "coordinator-heartbeat.json").write_text(
                    json.dumps(state), encoding="utf-8")
                print(json.dumps(state), flush=True)
                if model_waits >= 72:
                    (output_dir / "coordinator-alert.json").write_text(
                        json.dumps(state), encoding="utf-8")
                    raise CoordinatorRetryExhausted from exc
                sleep(300)
                continue
            model_waits = 0
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
    if max((row["id"] for row in runs), default=None) != run_id:
        return "STOP"
    if task["status"] in {"COMPLETED", "STOPPED", "FAILED"}:
        return "STOP"
    if task["status"] == "RUNNING":
        if run["status"] == "RUNNING":
            checkpoint = run.get("checkpoint") or {}
            active = checkpoint.get("ppa_batch") or {}
            cases = active.get("cases") or []
            prefetch_path = output_dir / f"run-{run_id}-prefetch.json"
            if len(cases) >= 2 and not prefetch_path.exists():
                evidence = request_json(f"{api}/runs/{run_id}/ppa-feedback")
                if evidence.get("next_phase") == active.get("phase"):
                    projected = evidence
                    if evidence.get("phase_limit") is not None:
                        phase = active["phase"]
                        persisted = {row.get("case_id") for row in evidence.get("rows", [])
                                     if isinstance(row, dict)}
                        remaining_active = sum(case.get("case_id") not in persisted
                                               for case in cases if isinstance(case, dict))
                        counts = dict(evidence.get("phase_counts") or {})
                        counts[phase] = counts.get(phase, 0) + remaining_active
                        if counts[phase] >= evidence["phase_limit"]:
                            return "WAIT"
                        projected = {**evidence, "phase_counts": counts}
                    prospective = prepare_with_policy(evidence["base"], projected, cockpit_script)
                    output_dir.mkdir(parents=True, exist_ok=True)
                    payload = {"epoch": evidence["epoch"], "phase": prospective["phase"],
                               "cases": prospective["cases"],
                               "candidate_generation": prospective["candidate_generation"]}
                    temporary = prefetch_path.with_suffix(".tmp")
                    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                    temporary.replace(prefetch_path)
            try:
                request_json(f"{api}/runs/{run_id}/ppa-qualification-reconcile", {})
            except (TimeoutError, URLError) as exc:
                if isinstance(exc, URLError) and not isinstance(exc.reason, TimeoutError):
                    raise
                return "WAIT_QUALIFICATION_TIMEOUT"
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
        cases = ((run.get("checkpoint") or {}).get("ppa_batch") or {}).get("cases") or []
        if not first_batch_reviewable(evidence, run_id, cases):
            return "WAIT_FIRST_BATCH_EVIDENCE_INCOMPLETE"
        request_json(f"{api}/tasks/{task_id}/intervene", {"action": "RESUME", "parameters": {"expected_run_id": run_id, "expected_block_reason": reason}})
        return "RESUMED_REVIEWED"
    if reason == "PPA_BATCH_READY":
        request_json(f"{api}/tasks/{task_id}/intervene", {"action": "RESUME", "parameters": {"expected_run_id": run_id, "expected_block_reason": reason}})
        return "RESUMED_BATCH"
    if reason == "PPA_REQUEST_RECONCILIATION_PENDING":
        recovered = request_json(f"{api}/runs/{run_id}/ppa-reconcile", {})
        return "WAIT_RECONCILE" if recovered["pending"] else "RECONCILED"
    if reason == "PPA_QUALIFICATION_EVIDENCE_PENDING":
        request_json(f"{api}/runs/{run_id}/ppa-qualification-reconcile", {})
        return "QUALIFICATION_RECONCILED"
    if reason == "PPA_RATE_LIMIT":
        retry_at = run.get("next_retry_at")
        if not retry_at:
            raise ValueError("PPA_RATE_LIMIT_RESET_UNKNOWN")
        reset_time = datetime.fromisoformat(retry_at)
        if reset_time.tzinfo is None:
            reset_time = reset_time.replace(tzinfo=timezone.utc)
        if reset_time > datetime.now(timezone.utc):
            return "WAIT_RATE_LIMIT"
        request_json(f"{api}/tasks/{task_id}/intervene", {"action": "RESUME", "parameters": {"expected_run_id": run_id, "expected_block_reason": reason}})
        return "RESUMED_RATE_LIMIT"
    if reason != "AWAITING_RESEARCH_BATCH":
        raise ValueError(f"PPA_REQUIRES_REVIEW:{reason}")
    evidence = request_json(f"{api}/runs/{run_id}/ppa-feedback")
    if evidence.get("unresolved_requests"):
        recovered = request_json(f"{api}/runs/{run_id}/ppa-reconcile", {})
        return "WAIT_RECONCILE" if recovered["pending"] else "RECONCILED"
    advances = 0
    while evidence["next_phase"] is None:
        if not evidence.get("continuous"):
            raise ValueError(f"PPA_NO_NEXT_PHASE:{evidence['stop_reason']}")
        if evidence.get("phase_counts") == {}:
            raise ValueError(f"PPA_NO_NEXT_PHASE:{evidence['stop_reason']}")
        if advances >= 32:
            return "ADVANCED_EPOCH"
        request_json(f"{api}/runs/{run_id}/ppa-epoch", {"expected_epoch": evidence["epoch"]})
        advances += 1
        evidence = request_json(f"{api}/runs/{run_id}/ppa-feedback")
        if evidence.get("unresolved_requests"):
            return "WAIT_RECONCILE"
    prefetch_path = output_dir / f"run-{run_id}-prefetch.json"
    batch = None
    if prefetch_path.exists():
        try:
            prospective = json.loads(prefetch_path.read_text(encoding="utf-8"))
            if (prospective.get("epoch") == evidence["epoch"]
                    and prospective.get("phase") == evidence["next_phase"]):
                generation = prospective.get("candidate_generation") or {}
                if generation.get("source") in {"cockpit_seed_local_expansion", "measured_seed_local_expansion",
                                                "measured_discovery_local_expansion"}:
                    if not evidence.get("local_expansion"):
                        raise ValueError("PPA_LOCAL_EXPANSION_DISABLED")
                    if generation["source"] == "measured_seed_local_expansion":
                        batch = freeze_measured_seed_expansion(evidence["base"], evidence)
                    elif generation["source"] == "measured_discovery_local_expansion":
                        batch = freeze_measured_discovery_expansion(evidence["base"], evidence)
                    else:
                        batch = freeze_seed_expansion(
                            evidence["base"], evidence, generation["seed_cases"],
                            model=generation["model"])
                else:
                    batch = freeze_batch(evidence["base"], evidence, prospective)
                batch["candidate_generation"].update({
                    key: value for key, value in generation.items()
                    if key in {"model", "generation_seconds", "validation_attempts", "top_up_calls"}})
        except (ValueError, TypeError, KeyError):
            batch = None
    if batch is None:
        batch = prepare_with_policy(evidence["base"], evidence, cockpit_script)
    output_dir.mkdir(parents=True, exist_ok=True)
    batch_path = output_dir / f"run-{run_id}-epoch-{evidence['epoch']}-{batch['phase']}-{batch['candidate_generation']['plan_sha256'][:12]}.json"
    batch_path.write_text(json.dumps(batch, indent=2, ensure_ascii=False), encoding="utf-8")
    request_json(f"{api}/runs/{run_id}/ppa-batch", batch)
    if prefetch_path.exists():
        prefetch_path.unlink()
    request_json(f"{api}/tasks/{task_id}/intervene", {"action": "RESUME", "parameters": {"expected_run_id": run_id, "expected_block_reason": "PPA_BATCH_READY"}})
    return f"IMPORTED:{batch_path.name}"


def activation_is_current(path, commit, task_id, run_id, cockpit_script, output_dir):
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        return (config.get("enabled") is True and config.get("commit") == commit
                and config.get("task_id") == task_id and config.get("run_id") == run_id
                and Path(config["cockpit_script"]) == cockpit_script
                and Path(config["output_dir"]) == output_dir)
    except (OSError, ValueError, TypeError, KeyError):
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", type=int, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--cockpit-script", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--api-base", default="http://127.0.0.1:8003/api/v1")
    parser.add_argument("--maintenance-file", type=Path)
    parser.add_argument("--activation-file", type=Path)
    parser.add_argument("--activation-commit")
    args = parser.parse_args(argv)
    if bool(args.activation_file) != bool(args.activation_commit):
        raise ValueError("PPA_COORDINATOR_ACTIVATION_INVALID")
    if args.api_base not in {"http://127.0.0.1:8003/api/v1", "http://localhost:8003/api/v1"}:
        raise ValueError("Only the local AIAC API is allowed")
    lease = acquire_lease(args.run_id)
    try:
        def enabled():
            return not (args.maintenance_file and args.maintenance_file.exists() or
                        args.activation_file and not activation_is_current(
                            args.activation_file, args.activation_commit, args.task_id, args.run_id,
                            args.cockpit_script, args.output_dir))
        while True:
            if not enabled():
                break
            try:
                action = run_step_with_retry(
                    lambda: step(args.api_base, args.task_id, args.run_id, args.cockpit_script, args.output_dir),
                    args.output_dir, enabled=enabled)
            except CoordinatorRetryExhausted:
                time.sleep(300)
                continue
            print(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "action": action}), flush=True)
            if action == "STOP":
                break
            time.sleep(60 if action in {"WAIT_RECONCILE", "WAIT_RATE_LIMIT", "WAIT_FIRST_BATCH_EVIDENCE_INCOMPLETE"} else 10)
    finally:
        lease.close()


if __name__ == "__main__":
    main()
