"""Fail CI when runtime files or recognizable credentials enter Git."""

from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_NAMES = {"keys.txt", "compose.local.yaml", ".env", "parsetab.py"}
FORBIDDEN_PARTS = {".cursor", ".venv", "node_modules", "dist", "work", "logs", "__pycache__"}
SECRET_PATTERNS = (
    re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"gh(?:p|o|u|s|r)_[A-Za-z0-9]{30,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def main() -> int:
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).split(b"\0")
    errors = []
    checked = 0
    for raw in tracked:
        if not raw:
            continue
        relative = raw.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        path = Path(relative)
        parts = set(path.parts)
        local_env_file = path.name.startswith(".env.") and path.name != ".env.example"
        private_key_file = path.suffix in {".pem", ".key", ".p12"}
        if (path.name in FORBIDDEN_NAMES or local_env_file or private_key_file
                or path.suffix == ".log" or parts & FORBIDDEN_PARTS):
            errors.append(f"forbidden tracked path: {relative}")
            continue
        source = ROOT / path
        if not source.is_file():
            continue
        content = source.read_bytes()
        checked += 1
        for pattern in SECRET_PATTERNS:
            if pattern.search(content):
                errors.append(f"possible credential in: {relative}")
                break
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"Repository hygiene failed: {len(errors)} issue(s).", file=sys.stderr)
        return 1
    print(f"Repository hygiene passed: {checked} tracked files checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
