"""Release worktrees must use the existing runtime home without copying secrets."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_manager_status_uses_explicit_runtime_home(tmp_path):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 unavailable")
    home = tmp_path / "work" / "runtime-home"
    home.mkdir(parents=True)
    script = Path(__file__).resolve().parents[3] / "scripts" / "manage-local.ps1"
    result = subprocess.run([pwsh, "-NoProfile", "-File", str(script), "-Action", "Status",
                             "-RuntimeHome", str(home)], capture_output=True, text=True, check=True)
    services = json.loads(result.stdout)
    assert {item["service"] for item in services} == {"api", "ui", "worker", "coordinator"}
    assert all(item["running"] is False for item in services)
    assert (tmp_path / "runtime" / "aiac-ops" / "pids").is_dir()
