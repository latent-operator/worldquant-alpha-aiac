"""Launch the pinned upstream API or UI while retaining a research hold.

Workers and beat are intentionally absent: old pending signatures must not replay.
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import sys

from scripts.clean_install import UPSTREAM_COMMIT, validate_source


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--service", choices=("api", "ui"), required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--api-port", type=int, default=8003)
    args = parser.parse_args()
    source = args.source.resolve()
    validate_source(source)
    from dotenv import load_dotenv
    load_dotenv(args.env_file, override=True)
    if os.environ.get("POSTGRES_DB") != "aiac_clean":
        raise ValueError("Clean runtime requires its isolated database")
    if args.service == "ui":
        from aiohttp import web
        from scripts.serve_ui import make_app
        web.run_app(make_app(args.env_file.resolve().parent / "ui-dist", UPSTREAM_COMMIT,
                            f"http://127.0.0.1:{args.api_port}"),
                    host="127.0.0.1", port=args.port, access_log=None)
    else:
        os.chdir(source)
        sys.path.insert(0, str(source))
        import uvicorn
        uvicorn.run("backend.main:app", host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
