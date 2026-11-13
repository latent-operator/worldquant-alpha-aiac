"""Run offline governance scanners and print counts, never finding contents."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import tempfile


def _scan(args, cwd):
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", check=False)
    except OSError:
        raise RuntimeError("governance scanner could not be started") from None
    if result.returncode not in (0, 1):
        raise RuntimeError("governance scanner execution failed")
    return result.stdout


def run(repo: Path, *, ruff: str = "ruff", knip: str = "knip", gitleaks: str = "gitleaks") -> dict:
    repo = Path(repo).resolve()
    try:
        ruff_findings = json.loads(_scan([ruff, "check", "backend", "scripts", "--no-cache",
                                          "--output-format", "json"], repo))
        knip_results = json.loads(_scan([knip, "--reporter", "json", "--no-progress"], repo / "frontend"))
        with tempfile.TemporaryDirectory(prefix="aiac-governance-") as temp:
            report = Path(temp) / "redacted.json"
            _scan([gitleaks, "git", "--log-opts=--all", "--redact=100", "--report-format", "json",
                   "--report-path", str(report), "--no-banner", "--no-color", "--log-level", "error"], repo)
            leaks = json.loads(report.read_text(encoding="utf-8"))
        return {"ruff": len(ruff_findings),
                "knip": sum(len(value) for issue in knip_results.get("issues", [])
                            for value in issue.values() if isinstance(value, list)),
                "gitleaks": len(leaks)}
    except (ValueError, OSError, TypeError, KeyError):
        raise RuntimeError("governance scanner output invalid") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--ruff", default="ruff")
    parser.add_argument("--knip", default="knip")
    parser.add_argument("--gitleaks", default="gitleaks")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.repo, ruff=args.ruff, knip=args.knip, gitleaks=args.gitleaks), sort_keys=True))
    except RuntimeError as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    main()
