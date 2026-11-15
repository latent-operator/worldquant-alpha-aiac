"""The operator CLI refuses unsafe input before lab or WQ writes."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from scripts.ppa_eval_experiment import (
    validate_platform_evidence, validate_lab_environment, private_output_path,
    production_state,
)


def test_platform_evidence_requires_current_matching_account_check():
    now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
    good = {"theme_id": "theme", "verified_at": "2026-11-13T11:00:00Z",
            "power_pool_access": True, "source_url": "https://platform.worldquantbrain.com/theme",
            "quota_remaining": 80}
    assert validate_platform_evidence(good, "theme", now)
    for change in ({"theme_id": "other"}, {"verified_at": "2026-11-11T00:00:00Z"},
                   {"power_pool_access": False}, {"quota_remaining": 0},
                   {"source_url": "https://example.com/theme"}):
        with pytest.raises(ValueError, match="PLATFORM_UNVERIFIED"):
            validate_platform_evidence({**good, **change}, "theme", now)

    # The existing PPA contract permits the account owner's no-cap policy.
    no_cap = {**good, "quota_remaining": None, "quota_mode": "NO_FIXED_LIMIT",
              "quota_source": "account_owner"}
    assert validate_platform_evidence(no_cap, "theme", now)
    with pytest.raises(ValueError, match="PLATFORM_UNVERIFIED"):
        validate_platform_evidence({**no_cap, "quota_source": "inferred"}, "theme", now)


def test_lab_environment_rejects_production_targets():
    good = {"AIAC_EVAL_LAB": "1", "POSTGRES_SERVER": "127.0.0.1",
            "POSTGRES_PORT": "55434", "POSTGRES_DB": "aiac_eval_ppa",
            "POSTGRES_USER": "aiac_eval", "POSTGRES_PASSWORD": "lab-secret",
            "BRAIN_EMAIL": "operator", "BRAIN_PASSWORD": "private"}
    assert validate_lab_environment(good)
    assert validate_lab_environment({**good, "POSTGRES_PORT": "55834"})
    for change in ({"POSTGRES_PORT": "55433"}, {"POSTGRES_DB": "alpha_gpt_local"},
                   {"AIAC_EVAL_LAB": "0"}, {"POSTGRES_SERVER": "example.com"},
                   {"BRAIN_PASSWORD": ""}):
        with pytest.raises(ValueError, match="LAB_ENV_INVALID"):
            validate_lab_environment({**good, **change})


def test_private_output_rejects_repository_paths(tmp_path):
    with pytest.raises(ValueError, match="PRIVATE_PATH_REQUIRED"):
        private_output_path(tmp_path / "repo" / "manifest.json", tmp_path / "repo")
    assert private_output_path(tmp_path / "private" / "manifest.json", tmp_path / "repo")


def test_production_gate_uses_only_get_and_counts_pending():
    calls = []
    def respond(request):
        calls.append(request.method)
        if request.url.path.endswith("research-report"):
            return httpx.Response(200, json={"request_counts": {"UNKNOWN": 2}})
        if request.url.path.endswith("tasks/14/summary"):
            return httpx.Response(200, json={"latest_run_id": 28, "status": "PAUSED"})
        return httpx.Response(200, json=[])
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        state = production_state(client, "http://127.0.0.1:8003/api/v1", 14, 28)
    assert state == {"source_unresolved": 2, "other_active_work": False,
                     "source_run_current": True}
    assert calls and set(calls) == {"GET"}


def test_cli_starts_from_repository_root_without_pythonpath():
    root = Path(__file__).resolve().parents[3]
    process = subprocess.run([sys.executable, "scripts/ppa_eval_experiment.py", "--help"],
                             cwd=root, capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
