"""Prepare a frozen theme PPA candidate list via the host-only Cockpit script.

This command does not call WQ or create an AIAC task. It never reads, prints,
or stores the Cockpit API key; cockpit-ask.ps1 owns local authentication.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
from urllib.parse import urlparse

from backend.services.theme_ppa import (
    account_quota_verified, candidate_plan_hash, validate_theme_task,
)


MODEL = "gpt-6-luna"
PROMPT_KEYS = (
    "theme_id", "theme_name", "platform_source_url", "starts_at", "ends_at", "region",
    "universe", "delay", "datasets", "dataset_categories", "allowed_operators",
    "max_fields", "max_operator_count", "settings", "confirmation_settings",
    "economic_mechanisms", "theme_requirements", "field_evidence",
)


def _prompt(base: dict) -> str:
    constraints = {key: base[key] for key in PROMPT_KEYS if key in base}
    return (
        "Generate research candidates for the verified WorldQuant Power Pool theme. "
        "Return only a JSON object with discovery, refinement, confirmation arrays. "
        "Discovery must contain exactly six pair_id groups, each with one CONTROL "
        "and three TREATMENT cases. Each treatment must change one conceptual "
        "factor from its control. Refinement may contain up to 40 cases; "
        "confirmation may contain up to 16. Every case needs a unique case_id, "
        "one dataset_id, and an expression using only listed fields and operators. "
        "Later cases need parent_case_id from an earlier phase. Do not add WQ "
        "results or claim that any expression is PPA eligible. Fixed settings may "
        "only be varied in confirmation using confirmation_settings. Use concise "
        "FastExpr expressions and valid JSON without markdown. Constraints:\n"
        + json.dumps(constraints, ensure_ascii=False, sort_keys=True)
    )


def _check_base(base: dict) -> None:
    source = urlparse(str(base.get("platform_source_url") or ""))
    if (base.get("verified_source") != "platform" or source.scheme != "https"
            or source.hostname != "platform.worldquantbrain.com"):
        raise ValueError("PPA_THEME_NOT_PLATFORM_VERIFIED")
    if not (base.get("theme_id") or base.get("theme_name")) or not base.get("datasets"):
        raise ValueError("PPA_MANIFEST_INCOMPLETE")
    eligibility = base.get("account_eligibility") or {}
    if (eligibility.get("power_pool_access") is not True
            or not account_quota_verified(eligibility)):
        raise ValueError("PPA_ACCOUNT_UNVERIFIED")


def prepare_manifest(base: dict, cockpit_script: Path, *, runner=subprocess.run) -> dict:
    """Ask Cockpit for all cases, validate locally, then return a frozen manifest."""
    _check_base(base)
    script = Path(cockpit_script).resolve()
    if not script.is_file():
        raise FileNotFoundError("Cockpit call script not found")
    with tempfile.TemporaryDirectory(prefix="aiac-ppa-cockpit-") as directory:
        prompt_path = Path(directory) / "prompt.txt"
        prompt_path.write_text(_prompt(base), encoding="utf-8")
        completed = runner(
            ["powershell.exe", "-NoProfile", "-File", str(script),
             "-PromptFile", str(prompt_path), "-Model", MODEL,
             "-MaxOutputTokens", "16000"],
            capture_output=True, text=True, encoding="utf-8", check=True,
            timeout=180,
        )
    try:
        plan = json.loads(completed.stdout.strip())
    except (json.JSONDecodeError, AttributeError) as exc:
        raise ValueError("COCKPIT_PLAN_INVALID_JSON") from exc
    if not isinstance(plan, dict) or set(plan) != {
            "discovery", "refinement", "confirmation"}:
        raise ValueError("COCKPIT_PLAN_INVALID_SHAPE")
    prepared = dict(base)
    prepared.update(plan)
    prepared["candidate_generation"] = {
        "source": "cockpit_local_responses",
        "model": MODEL,
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "plan_sha256": candidate_plan_hash(prepared),
    }
    task = SimpleNamespace(
        region=prepared.get("region"), universe=prepared.get("universe"),
        dataset_strategy="SPECIFIC",
        target_datasets=list(prepared.get("datasets") or {}),
        config={"research_protocol": "theme_ppa_v1", "research_request_budget": 80,
                "continuous_mode": False, "ppa_manifest": prepared},
    )
    reason = validate_theme_task(task)
    if reason:
        raise ValueError(reason)
    return prepared


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-base", required=True, type=Path)
    parser.add_argument("--cockpit-script", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.out.exists():
        parser.error("Output already exists; choose a new path")
    base = json.loads(args.manifest_base.read_text(encoding="utf-8"))
    if not isinstance(base, dict):
        parser.error("Manifest base must be a JSON object")
    prepared = prepare_manifest(base, args.cockpit_script)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(prepared, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(f"Prepared {args.out} (plan sha256 {prepared['candidate_generation']['plan_sha256']})")


if __name__ == "__main__":
    main()
