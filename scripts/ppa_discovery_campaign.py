"""Prepare and run private, audited PPA discovery batches without a total request cap."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.evals.discovery_campaign import (
    CATEGORY_ORDER, PREFERRED_DATASETS, choose_dataset, freeze_campaign_batch,
    run_campaign_batch, validate_campaign_batch,
)
from backend.services.ppa_feedback import behavioral_signature, case_settings, digest, prepare_batch
from backend.services.research_audit import signature
from scripts.ppa_eval_experiment import (
    DEFAULT_API, _exclusive_lock, _loopback_base, _require_clean_commit,
    _write_private, private_output_path, production_state, validate_lab_environment,
    validate_platform_evidence,
)

EXCLUDED_DATASETS = frozenset({"pv1", "model110", "predictive_starmine"})


def _read_private(path: str | Path) -> dict:
    return json.loads(private_output_path(Path(path)).read_text(encoding="utf-8"))


async def _old_ledger(maker, old_manifest: dict) -> dict:
    from sqlalchemy import select
    from backend.models import ExperimentRun, ResearchCandidate
    async with maker() as db:
        runs = (await db.execute(select(ExperimentRun))).scalars().all()
        matching = [run for run in runs if (run.checkpoint or {}).get("manifest_sha256") == old_manifest["manifest_sha256"]]
        if len(matching) != 1:
            raise ValueError("OLD_LAB_RUN_NOT_FOUND")
        old = matching[0]
        rows = (await db.execute(select(ResearchCandidate).where(
            ResearchCandidate.run_id == old.id))).scalars().all()
    unknown = [row for row in rows if row.request_status == "UNKNOWN"]
    if (len(unknown) != 45 or sum(row.request_status == "RESULT" for row in rows) != 35
            or old.task_id != old_manifest.get("old_lab_task_id", old.task_id)
            or any((row.result_snapshot or {}).get("request", {}).get("location") for row in unknown)):
        raise ValueError("OLD_LAB_LEDGER_CHANGED")
    dates = [datetime.fromisoformat((row.result_snapshot or {})["request"]["finished_at"])
             for row in unknown if (row.result_snapshot or {}).get("request", {}).get("finished_at")]
    if not dates:
        raise ValueError("OLD_ERROR_TIME_MISSING")
    last = max(dates)
    return {"task_id": old.task_id, "run_id": old.id, "last_error_at": last.isoformat(),
            "unknown_count": len(unknown), "unknown_expressions": [row.expression for row in unknown]}


async def _live_catalog(brain) -> tuple[list[dict], dict[str, list[dict]]]:
    """Page the account's actual WQ catalog; an empty or partial page fails closed."""
    datasets = []
    for offset in range(0, 10000, 50):
        response = await brain._safe_api_call("GET", "/data-sets", params={
            "region": "GLB", "delay": 1, "universe": "TOP3000",
            "instrumentType": "EQUITY", "limit": 50, "offset": offset})
        if response.status_code != 200:
            raise ValueError("LIVE_DATASET_QUERY_FAILED")
        payload = response.json()
        page = payload.get("results")
        if not isinstance(page, list):
            raise ValueError("LIVE_DATASET_QUERY_FAILED")
        datasets.extend(page)
        if len(page) < 50:
            break
    else:
        raise ValueError("LIVE_DATASET_QUERY_UNBOUNDED")
    if not datasets:
        raise ValueError("NO_LIVE_DATASETS")
    keyed = {str(item.get("id")): item for item in datasets if isinstance(item, dict)}
    fallback = sorted((item for item in keyed.values()
                       if item.get("category") in CATEGORY_ORDER),
                      key=lambda item: (CATEGORY_ORDER.index(item["category"]),
                                        -float(item.get("coverage") or 0), str(item["id"])))
    ids = [name for name in PREFERRED_DATASETS if name in keyed]
    ids.extend(str(item["id"]) for item in fallback if item["id"] not in ids)
    fields = {}
    for dataset_id in ids:
        if dataset_id in EXCLUDED_DATASETS:
            continue
        fields[dataset_id] = await brain.get_datafields(
            dataset_id, region="GLB", delay=1, universe="TOP3000")
        try:
            choose_dataset(datasets, {dataset_id: fields[dataset_id]}, excluded=EXCLUDED_DATASETS)
            break
        except ValueError:
            continue
    return datasets, fields


def _new_base(source: dict, selected: dict, *, verified_at: str) -> dict:
    base = dict(source)
    dataset_id = selected["dataset_id"]
    category = selected["category"] or "new_signal"
    base["datasets"] = {dataset_id: selected["field_ids"]}
    base["dataset_categories"] = {dataset_id: category}
    base["field_metadata"] = {dataset_id: {
        key: {**value, "verified_at": verified_at,
              "source_url": "https://api.worldquantbrain.com/data-fields"}
        for key, value in selected["field_metadata"].items()}}
    base["mechanisms"] = {"account_verified_signal": {
        "dataset_id": dataset_id,
        "hypothesis": f"Test whether an account-verified {category} signal predicts cross-sectional returns; specify the economic channel in every proposed group.",
        "direction": "MEASURE"}}
    base["discovery_allocation"] = {"account_verified_signal": 4}
    for key in ("field_evidence", "economic_mechanisms", "historical_mechanisms",
                "historical_mechanism_registry", "research_plan"):
        base.pop(key, None)
    return base


def _history(source_feedback: dict, old_manifest: dict, previous_batches: list[dict], base: dict) -> tuple[list, list]:
    settings = case_settings(base, {})
    identities = {tuple(item) for item in source_feedback.get("history_signatures") or []}
    behaviors = {tuple(item) for item in source_feedback.get("history_behavioral_signatures") or []}
    for case in [*(old_manifest.get("candidates") or []),
                 *(item for batch in previous_batches for item in batch["cases"])]:
        expression = case.get("expression")
        if not isinstance(expression, str):
            continue
        identities.add(signature(expression, settings))
        behaviors.add(behavioral_signature(expression, settings))
    return sorted(map(list, identities)), sorted(map(list, behaviors))


def _evidence(source: dict, old_manifest: dict, batches: list[dict], base: dict) -> dict:
    historical, behavioral = _history(source, old_manifest, batches, base)
    previous = [case for batch in batches for case in batch["cases"]]
    evidence = {"next_phase": "discovery", "phase_limit": 16,
                "phase_counts": {"discovery": 0}, "max_batch_cases": 16,
                "budget_remaining": None, "continuous": True,
                "rows": [], "seeds": [], "history_signatures": historical,
                "history_behavioral_signatures": behavioral,
                "exhausted_templates": [], "strict_max_operator_count": 5,
                "proposed_avoid_expressions": [case["expression"] for case in previous[-16:]],
                "stopped_families": []}
    evidence["evidence_hash"] = digest({"source": source.get("evidence_hash"),
                                        "history": historical, "behavioral": behavioral,
                                        "previous_batches": [batch["batch_sha256"] for batch in batches]})
    return evidence


def _source_feedback(base_url: str, task_id: int, run_id: int) -> dict:
    base_url = _loopback_base(base_url)
    with httpx.Client(timeout=30) as client:
        feedback = client.get(f"{base_url}/runs/{run_id}/ppa-feedback")
        feedback.raise_for_status()
        run = client.get(f"{base_url}/runs/{run_id}")
        run.raise_for_status()
    if run.json().get("task_id") != task_id:
        raise ValueError("SOURCE_TASK_RUN_MISMATCH")
    return feedback.json()


def _historical_baseline(base_url: str) -> dict:
    expected = {18: 212, 25: 16, 26: 20, 27: 28, 28: 9}
    rows = []
    with httpx.Client(timeout=30) as client:
        for run_id, count in expected.items():
            response = client.get(f"{_loopback_base(base_url)}/runs/{run_id}/research-report")
            response.raise_for_status()
            report = response.json()
            if (report.get("task_id") != 14 or
                    (report.get("request_counts") or {}).get("RESULT") != count or
                    (report.get("quality_counts") or {}).get("PASS", 0) != 0):
                raise ValueError("HISTORICAL_BASELINE_CHANGED")
            rows.append({"run_id": run_id, "result_count": count, "quality_pass_count": 0})
    return {"runs": rows, "result_count": sum(row["result_count"] for row in rows),
            "quality_pass_count": 0, "observed_at": datetime.now(timezone.utc).isoformat()}


def _campaign_paths(directory: Path) -> tuple[Path, list[Path]]:
    directory = private_output_path(directory)
    return directory / "campaign.json", sorted(directory.glob("batch-[0-9][0-9][0-9].json"))


def _load_campaign(directory: Path) -> tuple[dict, list[dict]]:
    campaign_file, paths = _campaign_paths(directory)
    campaign = _read_private(campaign_file)
    expected = digest({key: value for key, value in campaign.items() if key != "campaign_sha256"})
    if campaign.get("campaign_sha256") != expected:
        raise ValueError("CAMPAIGN_HASH_MISMATCH")
    batches = [_read_private(path) for path in paths]
    for index, batch in enumerate(batches, 1):
        validate_campaign_batch(campaign, batch)
        if batch["index"] != index:
            raise ValueError("CAMPAIGN_BATCH_SEQUENCE_INVALID")
    return campaign, batches


async def _prepare_new(args, feedback: dict, commit: str) -> tuple[dict, dict]:
    from backend.adapters.brain_adapter import BrainAdapter
    from backend.database import AsyncSessionLocal
    old_state = await _old_ledger(AsyncSessionLocal, args.old_manifest)
    if old_state["task_id"] != args.old_lab_task_id:
        raise ValueError("OLD_LAB_TASK_MISMATCH")
    async with BrainAdapter(email=os.environ["BRAIN_EMAIL"], password=os.environ["BRAIN_PASSWORD"]) as brain:
        catalog, fields = await _live_catalog(brain)
    selected = choose_dataset(catalog, fields, excluded=EXCLUDED_DATASETS)
    observed = datetime.now(timezone.utc).isoformat()
    base = _new_base(feedback["base"], selected, verified_at=observed)
    campaign = {"schema_version": 1,
                "campaign_id": digest(["discovery", commit, feedback["evidence_hash"], observed])[:32],
                "source_task_id": args.source_task_id, "source_run_id": args.source_run_id,
                "source_evidence_hash": feedback["evidence_hash"],
                "old_lab_task_id": args.old_lab_task_id,
                "old_manifest_sha256": args.old_manifest["manifest_sha256"],
                "old_unknown_count": 45, "old_lab_run_id": old_state["run_id"],
                "old_result_count": 35, "historical_baseline": args.historical_baseline,
                "old_last_error_at": old_state["last_error_at"],
                "old_quarantine_until": (datetime.fromisoformat(old_state["last_error_at"]) + timedelta(hours=24)).isoformat(),
                "dataset_id": selected["dataset_id"], "dataset_verified_at": observed,
                "code_commit": commit, "base": base}
    campaign["campaign_sha256"] = digest(campaign)
    evidence = _evidence(feedback, args.old_manifest, [], base)
    proposal = prepare_batch(base, evidence, args.cockpit_script)
    batch = freeze_campaign_batch(campaign, evidence, proposal, index=1)
    return campaign, batch


def _prepare(args) -> dict:
    commit = _require_clean_commit()
    validate_lab_environment(dict(os.environ))
    directory = private_output_path(Path(args.directory))
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("CAMPAIGN_DIRECTORY_NOT_EMPTY")
    old = _read_private(args.old_manifest)
    if (old.get("budget") != 80 or old.get("source_run_id") != args.source_run_id
            or old.get("source_task_id") != args.source_task_id
            or digest({key: value for key, value in old.items() if key != "manifest_sha256"}) != old.get("manifest_sha256")):
        raise ValueError("OLD_MANIFEST_MISMATCH")
    args.old_manifest = old
    feedback = _source_feedback(args.base_url, args.source_task_id, args.source_run_id)
    args.historical_baseline = _historical_baseline(args.base_url)
    campaign, batch = asyncio.run(_prepare_new(args, feedback, commit))
    directory.mkdir(parents=True, exist_ok=True)
    _write_private(directory / "campaign.json", campaign)
    _write_private(directory / "batch-001.json", batch)
    return {"status": "FROZEN", "campaign_id": campaign["campaign_id"],
            "dataset_id": campaign["dataset_id"], "batch_cases": len(batch["cases"]),
            "batch_sha256": batch["batch_sha256"], "directory": str(directory)}


def _state(campaign: dict, base_url: str, platform: dict) -> dict:
    with httpx.Client(timeout=15) as client:
        state = production_state(client, base_url, campaign["source_task_id"],
                                 campaign["source_run_id"])
        response = client.get(f"{_loopback_base(base_url)}/runs/{campaign['source_run_id']}/ppa-feedback")
        response.raise_for_status()
    if response.json().get("evidence_hash") != campaign["source_evidence_hash"]:
        state["source_run_current"] = False
    try:
        validate_platform_evidence(platform, campaign["base"]["theme_id"], datetime.now(timezone.utc))
        state["platform_verified"] = True
    except ValueError:
        state["platform_verified"] = False
    state["now"] = datetime.now(timezone.utc)
    return state


async def _old_inventory(brain, maker, campaign: dict) -> dict:
    from sqlalchemy import select
    from backend.models import ResearchCandidate
    old = await _old_ledger(maker, {"manifest_sha256": campaign["old_manifest_sha256"]})
    if old["run_id"] != campaign["old_lab_run_id"] or old["unknown_count"] != 45:
        raise ValueError("OLD_LAB_LEDGER_CHANGED")
    account = await brain._safe_api_call("GET", "/users/self")
    if account.status_code != 200:
        raise ValueError("WQ_ACCOUNT_AUTHENTICATION_FAILED")
    unknown_fingerprints = {"".join(expression.lower().split()) for expression in old["unknown_expressions"]}
    matched, read_count = [], 0
    # Read the recent account inventory. Missing expressions remain uncertain.
    for offset in range(0, 5000, 100):
        response = await brain._safe_api_call("GET", "/users/self/alphas",
                                              params={"limit": 100, "offset": offset,
                                                      "hidden": False, "order": "-dateCreated"})
        if response.status_code != 200:
            raise ValueError("WQ_ALPHA_INVENTORY_READ_FAILED")
        payload = response.json()
        page = payload.get("results") or []
        if not isinstance(page, list):
            raise ValueError("WQ_ALPHA_INVENTORY_INVALID")
        read_count += len(page)
        for item in page:
            expression = ((item.get("regular") or {}).get("code") or item.get("expression") or "")
            if expression and "".join(expression.lower().split()) in unknown_fingerprints:
                matched.append(item.get("id"))
        if len(page) < 100:
            break
    response = await brain._safe_api_call("GET", "/simulations", params={"limit": 100})
    active = []
    if response.status_code == 200:
        active = [item for item in response.json().get("results", [])
                  if str(item.get("status") or "").upper() in {"RUNNING", "PENDING", "CREATED"}]
    if matched or active:
        raise ValueError("OLD_REQUEST_OR_WQ_ACTIVITY_FOUND")
    return {"checked_at": datetime.now(timezone.utc).isoformat(),
            "account_authenticated": True, "old_unknown_retained": 45,
            "old_unknown_still_unreconciled": True,
            "alpha_records_read": read_count, "matched_old_alpha_ids": matched,
            "active_simulation_count": len(active),
            "active_endpoint_status": response.status_code}


def _run(args) -> dict:
    campaign, batches = _load_campaign(Path(args.directory))
    if _require_clean_commit() != campaign["code_commit"]:
        raise ValueError("CAMPAIGN_COMMIT_CHANGED")
    validate_lab_environment(dict(os.environ))
    now = datetime.now(timezone.utc)
    quarantine_until = datetime.fromisoformat(campaign["old_quarantine_until"])
    if now < quarantine_until:
        return {"status": "BLOCKED", "reason": "OLD_UNKNOWN_24H_WAIT",
                "not_before": quarantine_until.isoformat(), "sent": 0}
    platform = _read_private(args.platform_evidence)
    state = _state(campaign, args.base_url, platform)
    if not state["platform_verified"] or state["source_unresolved"] or state["other_active_work"] or not state["source_run_current"]:
        return {"status": "BLOCKED", "reason": "SOURCE_OR_PLATFORM_GATE", "sent": 0,
                "state": state}
    from backend.adapters.brain_adapter import BrainAdapter
    from backend.adapters.mcp_brain_adapter import MCPBrainAdapter
    from backend.database import AsyncSessionLocal

    async def execute():
        fallback = BrainAdapter(email=os.environ["BRAIN_EMAIL"], password=os.environ["BRAIN_PASSWORD"])
        async with fallback, MCPBrainAdapter(fallback=fallback) as brain:
            inventory = await _old_inventory(fallback, AsyncSessionLocal, campaign)
            # The Alpha inventory is evidence of account activity, not proof that
            # the 45 locationless requests failed. Keep them sealed in old run 1.
            completed = await _completed_batches(AsyncSessionLocal, campaign["campaign_id"])
            batch = next((item for item in batches if item["batch_sha256"] not in completed),
                         batches[-1] if batches else None)
            if batch is None:
                raise ValueError("NO_FROZEN_BATCH")
            def live_state():
                return _state(campaign, args.base_url, platform)
            total_sent = 0
            while True:
                result = await run_campaign_batch(campaign, batch, maker=AsyncSessionLocal,
                                                  brain=brain, live_state=live_state,
                                                  max_steps=args.max_steps)
                total_sent += result["sent"]
                if result["reason"] == "EVAL_RATE_LIMIT":
                    from backend.models import ExperimentRun
                    async with AsyncSessionLocal() as db:
                        run = await db.get(ExperimentRun, result["run_id"])
                        retry_at = run.next_retry_at
                    if retry_at:
                        if retry_at.tzinfo is None:
                            retry_at = retry_at.replace(tzinfo=timezone.utc)
                        await asyncio.sleep(max(1, min(86400, (retry_at - datetime.now(timezone.utc)).total_seconds())))
                    continue
                if result["reason"] != "EVAL_BATCH_COMPLETE":
                    return {**result, "total_sent_this_process": total_sent,
                            "old_inventory": inventory, "batch_sha256": batch["batch_sha256"]}
                feedback = _source_feedback(args.base_url, campaign["source_task_id"], campaign["source_run_id"])
                if feedback.get("evidence_hash") != campaign["source_evidence_hash"]:
                    return {"status": "PAUSED", "reason": "SOURCE_EVIDENCE_CHANGED",
                            "total_sent_this_process": total_sent}
                old = _read_private(args.old_manifest)
                if old.get("manifest_sha256") != campaign["old_manifest_sha256"]:
                    raise ValueError("OLD_MANIFEST_MISMATCH")
                evidence = _evidence(feedback, old, batches, campaign["base"])
                try:
                    proposal = prepare_batch(campaign["base"], evidence, args.cockpit_script)
                    batch = freeze_campaign_batch(campaign, evidence, proposal, index=len(batches) + 1)
                except (ValueError, OSError) as exc:
                    return {"status": "PAUSED", "reason": "NO_VALID_NEW_BATCH",
                            "error_type": type(exc).__name__,
                            "total_sent_this_process": total_sent}
                _write_private(Path(args.directory) / f"batch-{batch['index']:03d}.json", batch)
                batches.append(batch)
    with _exclusive_lock(Path(args.directory).resolve()):
        return asyncio.run(execute())


async def _completed_batches(maker, campaign_id: str) -> set[str]:
    from sqlalchemy import select
    from backend.models import ExperimentRun
    async with maker() as db:
        runs = (await db.execute(select(ExperimentRun))).scalars().all()
    matches = [run for run in runs if (run.checkpoint or {}).get("campaign_id") == campaign_id]
    if len(matches) > 1:
        raise ValueError("DUPLICATE_CAMPAIGN_RUN")
    return set((matches[0].checkpoint or {}).get("completed_batches", [])) if matches else set()


def _next(args) -> dict:
    campaign, batches = _load_campaign(Path(args.directory))
    if _require_clean_commit() != campaign["code_commit"]:
        raise ValueError("CAMPAIGN_COMMIT_CHANGED")
    validate_lab_environment(dict(os.environ))
    from backend.database import AsyncSessionLocal
    completed = asyncio.run(_completed_batches(AsyncSessionLocal, campaign["campaign_id"]))
    if not batches or batches[-1]["batch_sha256"] not in completed:
        raise ValueError("PREVIOUS_BATCH_NOT_COMPLETE")
    feedback = _source_feedback(args.base_url, campaign["source_task_id"], campaign["source_run_id"])
    if feedback["evidence_hash"] != campaign["source_evidence_hash"]:
        raise ValueError("SOURCE_EVIDENCE_CHANGED")
    old = _read_private(args.old_manifest)
    if old.get("manifest_sha256") != campaign["old_manifest_sha256"]:
        raise ValueError("OLD_MANIFEST_MISMATCH")
    evidence = _evidence(feedback, old, batches, campaign["base"])
    proposal = prepare_batch(campaign["base"], evidence, args.cockpit_script)
    batch = freeze_campaign_batch(campaign, evidence, proposal, index=len(batches) + 1)
    path = Path(args.directory) / f"batch-{batch['index']:03d}.json"
    _write_private(path, batch)
    return {"status": "FROZEN", "batch_sha256": batch["batch_sha256"],
            "batch_cases": len(batch["cases"]), "batch_index": batch["index"]}


def _report(args) -> dict:
    campaign, batches = _load_campaign(Path(args.directory))
    validate_lab_environment(dict(os.environ), require_brain=False)
    from backend.database import AsyncSessionLocal
    from backend.models import ExperimentRun, ResearchCandidate
    from sqlalchemy import select

    async def read():
        async with AsyncSessionLocal() as db:
            runs = (await db.execute(select(ExperimentRun))).scalars().all()
            matching = [run for run in runs if (run.checkpoint or {}).get("campaign_id") == campaign["campaign_id"]]
            if len(matching) != 1:
                raise ValueError("CAMPAIGN_RUN_NOT_FOUND")
            run = matching[0]
            rows = (await db.execute(select(ResearchCandidate).where(
                ResearchCandidate.run_id == run.id).order_by(ResearchCandidate.id))).scalars().all()
            by_case = {case["case_id"]: index for index, batch in enumerate(batches, 1)
                       for case in batch["cases"]}
            return run, [{"batch": by_case.get(row.experiment_group),
                          "case_id": row.experiment_group, "candidate_id": row.id,
                          "request_status": row.request_status, "wq_alpha_id": row.wq_alpha_id,
                          "fitness": (row.metrics or {}).get("fitness"),
                          "margin": (row.metrics or {}).get("margin"),
                          "quality_status": row.quality_status,
                          "platform_ppa_status": (row.result_snapshot or {}).get("platform_ppa_evaluation", {}).get("status"),
                          "ppa_status": (row.result_snapshot or {}).get("ppa_evaluation", {}).get("status")}
                         for row in rows]

    run, rows = asyncio.run(read())
    payload = {"campaign_id": campaign["campaign_id"], "campaign_sha256": campaign["campaign_sha256"],
               "source_run_id": campaign["source_run_id"], "old_unknown_retained": 45,
               "run_id": run.id, "status": run.status, "block_reason": run.block_reason,
               "batches": [{"index": batch["index"], "batch_sha256": batch["batch_sha256"]}
                           for batch in batches], "candidate_lineage": rows}
    target = Path(args.directory) / "report.json"
    if target.exists():
        target = Path(args.directory) / f"report-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    _write_private(target, payload)
    return {"status": run.status, "reason": run.block_reason,
            "result_count": sum(row["request_status"] == "RESULT" for row in rows),
            "unknown_count": sum(row["request_status"] == "UNKNOWN" for row in rows),
            "quality_pass_count": sum(row["quality_status"] == "PASS" for row in rows),
            "report_path": str(target)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--directory", required=True)
    prepare.add_argument("--old-manifest", required=True)
    prepare.add_argument("--old-lab-task-id", type=int, default=1)
    prepare.add_argument("--source-task-id", type=int, default=14)
    prepare.add_argument("--source-run-id", type=int, default=28)
    prepare.add_argument("--base-url", default=DEFAULT_API)
    prepare.add_argument("--cockpit-script", default="G:/安装/claude/tools/cockpit-ask.ps1")
    run = sub.add_parser("run")
    run.add_argument("--directory", required=True)
    run.add_argument("--old-manifest", required=True)
    run.add_argument("--platform-evidence", required=True)
    run.add_argument("--base-url", default=DEFAULT_API)
    run.add_argument("--cockpit-script", default="G:/安装/claude/tools/cockpit-ask.ps1")
    run.add_argument("--max-steps", type=int)
    next_batch = sub.add_parser("next")
    next_batch.add_argument("--directory", required=True)
    next_batch.add_argument("--old-manifest", required=True)
    next_batch.add_argument("--base-url", default=DEFAULT_API)
    next_batch.add_argument("--cockpit-script", default="G:/安装/claude/tools/cockpit-ask.ps1")
    report = sub.add_parser("report")
    report.add_argument("--directory", required=True)
    args = parser.parse_args()
    try:
        result = {"prepare": _prepare, "run": _run, "next": _next,
                  "report": _report}[args.command](args)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except (OSError, ValueError, KeyError, httpx.HTTPError) as exc:
        # Model errors can include private expressions. Emit only a short class code.
        code = str(exc).split(":", 1)[0]
        print(json.dumps({"status": "BLOCKED", "reason": code}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
