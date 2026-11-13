"""Prepare, gate, run, and report an isolated 80-request PPA backtest."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.evals.live_ppa import dispatch_gate, freeze_experiment
from backend.services.ppa_feedback import digest
from backend.services.theme_ppa import account_quota_verified


DEFAULT_API = "http://127.0.0.1:8003/api/v1"


def _loopback_base(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("PRODUCTION_API_MUST_BE_LOOPBACK")
    return base_url.rstrip("/")


def private_output_path(path: Path, root: Path = ROOT) -> Path:
    target = Path(path).resolve()
    if target.is_relative_to(Path(root).resolve()):
        raise ValueError("PRIVATE_PATH_REQUIRED")
    return target


def _write_private(path: Path, payload: dict) -> None:
    path = private_output_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _read_manifest(path: Path) -> dict:
    manifest = json.loads(private_output_path(path).read_text(encoding="utf-8"))
    expected = digest({key: value for key, value in manifest.items() if key != "manifest_sha256"})
    if expected != manifest.get("manifest_sha256"):
        raise ValueError("MANIFEST_HASH_MISMATCH")
    return manifest


def _current_commit() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                          capture_output=True, text=True, check=True).stdout.strip()


def _require_clean_commit() -> str:
    commit = _current_commit()
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"],
                           cwd=ROOT, capture_output=True, text=True, check=True).stdout
    if dirty:
        raise ValueError("COMMIT_NOT_CLEAN")
    return commit


def validate_platform_evidence(evidence: dict, theme_id: str, now: datetime) -> bool:
    try:
        observed = datetime.fromisoformat(evidence["verified_at"].replace("Z", "+00:00"))
        url = urlparse(evidence["source_url"])
        valid = (observed.tzinfo is not None and now - timedelta(hours=24) <= observed <= now
                 and evidence.get("theme_id") == theme_id
                 and evidence.get("power_pool_access") is True
                 and url.scheme == "https" and url.hostname == "platform.worldquantbrain.com"
                 and account_quota_verified(evidence))
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("PLATFORM_UNVERIFIED")
    return True


def validate_lab_environment(env: dict[str, str], *, require_brain: bool = True) -> bool:
    valid = (env.get("AIAC_EVAL_LAB") == "1"
             and env.get("POSTGRES_SERVER") in {"127.0.0.1", "localhost"}
             and env.get("POSTGRES_PORT") in {"55434", "55834"}
             and env.get("POSTGRES_DB") == "aiac_eval_ppa"
             and env.get("POSTGRES_USER") == "aiac_eval"
             and bool(env.get("POSTGRES_PASSWORD"))
             and (not require_brain or bool(env.get("BRAIN_EMAIL")) and bool(env.get("BRAIN_PASSWORD")))
             and env.get("AIAC_OFFLINE_CHECK") != "1")
    if not valid:
        raise ValueError("LAB_ENV_INVALID")
    return True


def production_state(client: httpx.Client, base_url: str, task_id: int, run_id: int) -> dict:
    base = _loopback_base(base_url)
    report = client.get(f"{base}/runs/{run_id}/research-report")
    report.raise_for_status()
    summary = client.get(f"{base}/tasks/{task_id}/summary")
    summary.raise_for_status()
    active = []
    for offset in range(0, 10000, 100):
        response = client.get(f"{base}/tasks", params={"status": "RUNNING", "limit": 100,
                                                        "offset": offset})
        response.raise_for_status()
        page = response.json()
        active.extend(page)
        if len(page) < 100:
            break
    else:
        raise ValueError("ACTIVE_TASK_LIST_UNBOUNDED")
    counts = report.json().get("request_counts") or {}
    task = summary.json()
    return {"source_unresolved": sum(int(counts.get(key, 0)) for key in
                                     ("RESERVED", "REQUESTED", "UNKNOWN")),
            "other_active_work": bool(active),
            "source_run_current": task.get("latest_run_id") == run_id
                                  and task.get("status") in {"PAUSED", "STOPPED"}}


@contextmanager
def _exclusive_lock(directory: Path):
    lock = directory / "ppa-eval-run.lock"
    descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


def _prepare(args) -> dict:
    base = _loopback_base(args.base_url)
    with httpx.Client(timeout=30) as client:
        feedback_response = client.get(f"{base}/runs/{args.source_run_id}/ppa-feedback")
        feedback_response.raise_for_status()
        run_response = client.get(f"{base}/runs/{args.source_run_id}")
        run_response.raise_for_status()
        feedback = feedback_response.json()
        run = run_response.json()
    if run.get("task_id") != args.source_task_id:
        raise ValueError("SOURCE_TASK_RUN_MISMATCH")
    snapshot_base = ((((run.get("config_snapshot") or {}).get("task") or {}).get("config") or {})
                     .get("ppa_manifest") or {})
    if snapshot_base.get("theme_id") != feedback["base"].get("theme_id"):
        raise ValueError("SOURCE_THEME_CHANGED")
    commit = _require_clean_commit()
    now = datetime.now(timezone.utc).isoformat()
    manifest = freeze_experiment(feedback, source_run_id=args.source_run_id,
        source_task_id=args.source_task_id, prepare_commit=commit, prepared_at=now)
    target = private_output_path(Path(args.output) if args.output else
        Path(tempfile.mkdtemp(prefix="aiac-ppa-eval-")) / "manifest.json")
    _write_private(target, manifest)
    return {"manifest_path": str(target), "manifest_sha256": manifest["manifest_sha256"],
            "candidate_count": len(manifest["candidates"]),
            "parent_count": len({case["parent_candidate_id"] for case in manifest["candidates"]}),
            "top20_exclusive": len(set(manifest["evidence_order"][:20])
                                   - set(manifest["baseline_order"][:20]))}


def _check_state(manifest: dict, args, evidence: dict | None = None) -> dict:
    with httpx.Client(timeout=15) as client:
        state = production_state(client, args.base_url,
                                 manifest["source_task_id"], manifest["source_run_id"])
        feedback = client.get(f"{_loopback_base(args.base_url)}/runs/"
                              f"{manifest['source_run_id']}/ppa-feedback")
        feedback.raise_for_status()
    if feedback.json().get("evidence_hash") != manifest.get("source_evidence_hash"):
        state["source_run_current"] = False
    if evidence is not None:
        try:
            validate_platform_evidence(evidence, manifest["base"]["theme_id"],
                                       datetime.now(timezone.utc))
            state["platform_verified"] = True
        except ValueError:
            state["platform_verified"] = False
    else:
        state["platform_verified"] = False
    state.update(lab_database="aiac_eval_ppa", lab_port=int(os.getenv("POSTGRES_PORT", "55434")),
                 theme_ends_at=manifest["base"]["ends_at"],
                 now=datetime.now(timezone.utc), existing_requests=0,
                 lab_unresolved=0, budget=manifest["budget"])
    return state


def _run(args, manifest: dict) -> dict:
    runtime_commit = _require_clean_commit()
    validate_lab_environment(dict(os.environ))
    if runtime_commit != manifest["prepare_commit"]:
        if not args.resume_existing or not asyncio.run(_existing_lab_run_has_requests(manifest)):
            raise ValueError("PREPARE_COMMIT_CHANGED")
    if manifest["budget"] != 80 or manifest["top_k"] != 20:
        raise ValueError("LIVE_EXPERIMENT_REQUIRES_FROZEN_80")
    evidence = json.loads(private_output_path(Path(args.platform_evidence)).read_text(encoding="utf-8"))
    initial = _check_state(manifest, args, evidence)
    reason = dispatch_gate(initial)
    if reason != "READY":
        return {"status": "BLOCKED", "reason": reason, "sent": 0}
    from backend.adapters.brain_adapter import BrainAdapter
    from backend.adapters.mcp_brain_adapter import MCPBrainAdapter
    from backend.database import AsyncSessionLocal
    from backend.evals.live_runner import run_lab

    def live_state():
        current = _check_state(manifest, args, evidence)
        return {key: current[key] for key in ("source_unresolved", "other_active_work",
                                              "source_run_current", "platform_verified", "now")}

    async def execute():
        fallback = BrainAdapter(email=os.environ["BRAIN_EMAIL"], password=os.environ["BRAIN_PASSWORD"])
        async with MCPBrainAdapter(fallback=fallback) as brain:
            return await run_lab(manifest, maker=AsyncSessionLocal, brain=brain,
                                 live_state=live_state, max_steps=args.max_steps,
                                 lab_port=int(os.environ["POSTGRES_PORT"]))

    with _exclusive_lock(Path(args.manifest).resolve().parent):
        return asyncio.run(execute())


async def _existing_lab_run_has_requests(manifest: dict) -> bool:
    from sqlalchemy import func, select
    from backend.database import AsyncSessionLocal
    from backend.models import ExperimentRun, ResearchCandidate
    async with AsyncSessionLocal() as db:
        runs = (await db.execute(select(ExperimentRun))).scalars().all()
        matching = [run for run in runs if (run.checkpoint or {}).get("manifest_sha256") == manifest["manifest_sha256"]]
        if len(matching) != 1:
            return False
        count = (await db.execute(select(func.count(ResearchCandidate.id)).where(
            ResearchCandidate.run_id == matching[0].id,
            ResearchCandidate.request_status.in_(("RESERVED", "REQUESTED", "UNKNOWN", "FAILED", "RESULT"))))).scalar_one()
        return count > 0


async def _lab_rows(manifest: dict) -> tuple[int, list[dict]]:
    from sqlalchemy import select
    from backend.database import AsyncSessionLocal
    from backend.models import Alpha, ExperimentRun, ResearchCandidate
    from backend.services.ppa_feedback import research_row
    from backend.services.ppa_research_policy import evidence_metrics, effective_improvement

    async with AsyncSessionLocal() as db:
        runs = (await db.execute(select(ExperimentRun))).scalars().all()
        matches = [run for run in runs if (run.checkpoint or {}).get("manifest_sha256") == manifest["manifest_sha256"]]
        if len(matches) != 1:
            raise ValueError("LAB_RUN_NOT_FOUND")
        run = matches[0]
        candidates = (await db.execute(select(ResearchCandidate).where(
            ResearchCandidate.run_id == run.id))).scalars().all()
        alphas = (await db.execute(select(Alpha).where(Alpha.run_id == run.id))).scalars().all()
        qualities = {alpha.alpha_id: alpha.quality_status for alpha in alphas}
        cases = {case["case_id"]: case for case in manifest["candidates"]}
        rows = []
        for candidate in candidates:
            case = cases.get(candidate.experiment_group)
            if case is None:
                raise ValueError("LAB_CASE_NOT_IN_MANIFEST")
            current = research_row(candidate)
            parent = manifest["parent_evidence"][str(case["parent_source_candidate_id"])]
            rows.append({"case_id": candidate.experiment_group, "candidate_id": candidate.id,
                "run_id": run.id, "parent_source_candidate_id": case["parent_source_candidate_id"],
                "wq_alpha_id": candidate.wq_alpha_id, "request_status": candidate.request_status,
                "quality_status": qualities.get(candidate.wq_alpha_id),
                "ppa_status": (candidate.result_snapshot or {}).get("ppa_evaluation", {}).get("status"),
                "platform_ppa_status": (candidate.result_snapshot or {}).get("platform_ppa_evaluation", {}).get("status"),
                "effective_improvement": bool(candidate.request_status == "RESULT" and
                                               effective_improvement(parent, current)),
                "test_sharpe": evidence_metrics(current).get("test_sharpe")})
        return run.id, rows


def _report(args, manifest: dict) -> dict:
    validate_lab_environment(dict(os.environ), require_brain=False)
    from backend.evals.live_ppa import analyze_results
    run_id, rows = asyncio.run(_lab_rows(manifest))
    analysis = analyze_results(manifest, rows)
    payload = {"manifest_sha256": manifest["manifest_sha256"], "run_id": run_id,
               "source_task_id": manifest["source_task_id"],
               "source_run_id": manifest["source_run_id"],
               "prepared_at": manifest["prepared_at"], "analysis": analysis,
               "candidate_lineage": rows}
    target = private_output_path(Path(args.output) if args.output else
        Path(args.manifest).resolve().parent / "report.json")
    _write_private(target, payload)
    return {"report_path": str(target), "run_id": run_id,
            "outcome": analysis["outcome"], "result_count": analysis["result_count"],
            "unknown_count": analysis["unknown_count"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--source-task-id", type=int, default=14)
    prepare.add_argument("--source-run-id", type=int, default=28)
    prepare.add_argument("--base-url", default=DEFAULT_API)
    prepare.add_argument("--output")
    status = sub.add_parser("status")
    status.add_argument("--manifest", required=True)
    status.add_argument("--base-url", default=DEFAULT_API)
    status.add_argument("--platform-evidence")
    run = sub.add_parser("run")
    run.add_argument("--manifest", required=True)
    run.add_argument("--platform-evidence", required=True)
    run.add_argument("--base-url", default=DEFAULT_API)
    run.add_argument("--max-steps", type=int, default=16)
    run.add_argument("--resume-existing", action="store_true",
                     help="Resume a populated frozen lab run after a CI-verified runtime-only change")
    report = sub.add_parser("report")
    report.add_argument("--manifest", required=True)
    report.add_argument("--output")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = _prepare(args)
        else:
            manifest = _read_manifest(Path(args.manifest))
            if args.command == "status":
                evidence = (json.loads(private_output_path(Path(args.platform_evidence)).read_text(encoding="utf-8"))
                            if args.platform_evidence else None)
                state = _check_state(manifest, args, evidence)
                result = {"status": dispatch_gate(state), "source_unresolved": state["source_unresolved"],
                          "other_active_work": state["other_active_work"],
                          "source_run_current": state["source_run_current"],
                          "platform_verified": state["platform_verified"]}
            elif args.command == "run":
                result = _run(args, manifest)
            else:
                result = _report(args, manifest)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, httpx.HTTPError) as exc:
        # Errors from external evidence and the validator may contain expressions.
        code = str(exc).split(":", 1)[0]
        print(json.dumps({"status": "BLOCKED", "reason": code}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
