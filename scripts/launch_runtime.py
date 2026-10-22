"""Launch one versioned AIAC host service with a shared private .env."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", choices=("api", "worker", "ui"), required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--commit", required=True)
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
    else:
        from aiohttp import web
        web.run_app(
            make_app(source / "frontend" / "dist", args.commit),
            host="127.0.0.1", port=5174, access_log=None,
        )


if __name__ == "__main__":
    main()
