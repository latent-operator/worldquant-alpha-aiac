from pathlib import Path
import subprocess
import sys


def test_host_script_is_runnable_from_repo_root():
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run([sys.executable, "scripts/advance_theme_ppa.py", "--help"],
                            cwd=root, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "--run-id" in result.stdout
