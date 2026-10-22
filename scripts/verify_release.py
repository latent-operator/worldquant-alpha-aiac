"""Fail closed unless the exact local commit passed CI on the user's fork."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
FORK = "latent-operator/worldquant-alpha-aiac"
LEGACY_DATASET_KEYS_BLOB = "9a3ce503dd74e397ed75f652e132ed97347a0b25"


def validate_ci_run(record: dict, commit: str) -> None:
    if record.get("repository", {}).get("full_name") != FORK:
        raise ValueError("CI run is not from the writable fork")
    if record.get("head_sha") != commit:
        raise ValueError("CI run does not match the requested commit")
    if record.get("name") != "AIAC offline checks" or record.get("status") != "completed" or record.get("conclusion") != "success":
        raise ValueError("A successful AIAC offline checks run is required")
    if record.get("head_branch") != "master" or record.get("event") != "push":
        raise ValueError("CI must be the master push after PR integration")


def validate_legacy_dataset_keys_blob(blob_id: str) -> None:
    """The reviewed historical keys.txt contains dataset names, not secrets."""
    if blob_id != LEGACY_DATASET_KEYS_BLOB:
        raise ValueError("The legacy keys.txt blob differs from the reviewed dataset list")


def _output(args: list[str]) -> str:
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError(f"Release check command failed: {args[0]}")
    return result.stdout.strip()


def verify(commit: str, ci_run: int) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Commit must be a full lowercase SHA")
    if _output(["git", "rev-parse", "HEAD"]) != commit:
        raise ValueError("Working checkout HEAD is not the requested commit")
    if _output(["git", "status", "--porcelain=v1"]):
        raise ValueError("Release requires a clean working tree")
    validate_legacy_dataset_keys_blob(_output(["git", "rev-parse", "c432aa5:keys.txt"]))
    record = json.loads(_output(["gh", "api", f"repos/{FORK}/actions/runs/{ci_run}"]))
    validate_ci_run(record, commit)
    return {
        "commit": commit,
        "ci_run": ci_run,
        "ci_url": record.get("html_url"),
        "repository": FORK,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--ci-run", type=int, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.commit, args.ci_run), sort_keys=True))
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Release blocked: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
