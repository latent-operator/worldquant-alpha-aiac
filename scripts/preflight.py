"""Run the same offline checks before commits and in CI."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Check:
    label: str
    argv: tuple[str, ...]
    cwd: Path


def command_plan() -> list[Check]:
    npm = "npm.cmd" if os.name == "nt" else "npm"
    return [
        Check("git diff", ("git", "diff", "--check"), ROOT),
        Check("staged git diff", ("git", "diff", "--cached", "--check"), ROOT),
        Check("repository hygiene", (sys.executable, "scripts/check_repo_hygiene.py"), ROOT),
        Check("backend unit tests", (sys.executable, "-m", "pytest", "backend/tests/unit", "-q", "--disable-warnings"), ROOT),
        Check("frontend tests", (npm, "test"), ROOT / "frontend"),
        Check("frontend lint", (npm, "run", "lint"), ROOT / "frontend"),
        Check("frontend build", (npm, "run", "build"), ROOT / "frontend"),
    ]


def offline_environment(source: dict[str, str]) -> dict[str, str]:
    env = dict(source)
    for name in ("BRAIN_EMAIL", "BRAIN_PASSWORD", "OPENAI_API_KEY", "CREDENTIALS_SECRET"):
        env[name] = ""
    env["POSTGRES_PORT"] = "1"
    env["REDIS_URL"] = "redis://127.0.0.1:1/0"
    env["AIAC_OFFLINE_CHECK"] = "1"
    return env


def main() -> int:
    env = offline_environment(os.environ)
    for check in command_plan():
        print(f"Running {check.label}...", flush=True)
        result = subprocess.run(check.argv, cwd=check.cwd, env=env, check=False)
        if result.returncode:
            print(f"Failed: {check.label} (exit {result.returncode})", file=sys.stderr)
            return result.returncode
    print("AIAC offline preflight passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
