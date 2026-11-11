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


def test_manager_starts_launcher_from_released_source(tmp_path):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 unavailable")
    home = tmp_path / "work" / "runtime-home"
    python = home / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    (home / ".env").touch()
    source = tmp_path / "released-source"
    dist = source / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").touch()
    marker = tmp_path / "working-directory.txt"
    script = Path(__file__).resolve().parents[3] / "scripts" / "manage-local.ps1"
    wrapper = tmp_path / "manager-test.ps1"
    wrapper.write_text('''param($Manager, $RuntimeHome, $Source, $Marker)
function docker { $global:LASTEXITCODE = 0 }
function Get-CimInstance { return @() }
function Start-Sleep { }
function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle, [switch]$PassThru,
          $RedirectStandardOutput, $RedirectStandardError)
    Set-Content -LiteralPath $Marker -Value $WorkingDirectory
    return [pscustomobject]@{ HasExited = $false; Id = 12345 }
}
& $Manager -Action Start -RuntimeHome $RuntimeHome -Source $Source -Commit ('a' * 40) -Services api
''', encoding="utf-8")
    subprocess.run([pwsh, "-NoProfile", "-File", str(wrapper), str(script), str(home),
                    str(source), str(marker)], capture_output=True, text=True, check=True)
    assert Path(marker.read_text(encoding="utf-8-sig").strip()) == source.resolve()
