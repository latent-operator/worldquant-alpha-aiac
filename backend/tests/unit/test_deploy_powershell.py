"""Windows release parameters must accept real GitHub Actions run IDs."""

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 unavailable")
def test_deploy_script_accepts_64_bit_ci_run_ids():
    command = (
        "$parameter=(Get-Command ./scripts/deploy-local.ps1).Parameters['CiRun']; "
        "if ($parameter.ParameterType -ne [long]) { exit 1 }"
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
