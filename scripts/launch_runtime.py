"""Launch one versioned AIAC host service with a shared private .env."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import runpy
import sys

from dotenv import load_dotenv
from scripts.serve_ui import make_app


def validate_source(source: Path) -> None:
    if not (source / "backend" / "main.py").is_file():
        raise ValueError("Released backend source is missing")
    if not (source / "frontend" / "dist" / "index.html").is_file():
        raise ValueError("Released frontend index.html is missing")


def load_runtime_env(env_file: Path) -> None:
    if not env_file.is_file():
        raise ValueError("Private runtime .env is missing")
    load_dotenv(env_file, override=True)
    if not os.environ.get("CREDENTIALS_SECRET"):
        raise ValueError("CREDENTIALS_SECRET is required for API and worker")


def load_coordinator_config(path: Path, commit: str) -> dict:
    """A coordinator starts only with an explicit, release-bound task activation."""
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        script = Path(config["cockpit_script"])
        output = Path(config["output_dir"])
        if (config.get("enabled") is not True or config.get("task_id") != 14
                or type(config.get("run_id")) is not int or not 1 <= config["run_id"] <= 18535
                or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit)
                or config.get("commit") != commit or not script.is_absolute() or not script.is_file()
                or not output.is_absolute()):
            raise ValueError
        return config
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("PPA_COORDINATOR_ACTIVATION_INVALID") from exc


def load_coordinator_main(source: Path):
    return runpy.run_path(str(source / "scripts" / "watch_theme_ppa.py"))["main"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", choices=("api", "worker", "ui", "coordinator"), required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--coordinator-config", type=Path)
    args = parser.parse_args()

    source = args.source.resolve()
    validate_source(source)
    load_runtime_env(args.env_file.resolve())
    os.chdir(source)
    sys.path.insert(0, str(source))

    if args.service == "api":
        import uvicorn
        uvicorn.run("backend.main:app", host="127.0.0.1", port=8003, access_log=False)
    elif args.service == "worker":
        from backend.celery_app import celery_app
        celery_app.worker_main(["worker", "--loglevel=info", "--pool=solo", "--concurrency=1"])
    elif args.service == "coordinator":
        if args.coordinator_config is None:
            raise ValueError("PPA_COORDINATOR_ACTIVATION_INVALID")
        config = load_coordinator_config(args.coordinator_config, args.commit)
        coordinator_main = load_coordinator_main(source)
        coordinator_main(["--task-id", str(config["task_id"]), "--run-id", str(config["run_id"]),
                          "--cockpit-script", config["cockpit_script"], "--output-dir", config["output_dir"],
                          "--maintenance-file", str(args.coordinator_config.parent / "maintenance.lock"),
                          "--activation-file", str(args.coordinator_config), "--activation-commit", args.commit])
    else:
        from aiohttp import web
        web.run_app(
            make_app(source / "frontend" / "dist", args.commit),
            host="127.0.0.1", port=5174, access_log=None,
        )


if __name__ == "__main__":
    main()
