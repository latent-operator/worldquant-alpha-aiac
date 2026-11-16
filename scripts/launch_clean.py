"""Launch pinned AIAC services, optionally with the audited bounded worker build."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys

from scripts.clean_install import UPSTREAM_COMMIT, validate_source


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--service", choices=("api", "ui", "worker"), required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--api-port", type=int, default=8003)
    args = parser.parse_args()
    source = args.source.resolve()
    bounded=(source/'bounded-build.json').exists()
    if bounded:
        from scripts.build_clean_runtime import validate_build
        validate_build(source)
    else:
        validate_source(source)
    from dotenv import load_dotenv
    load_dotenv(args.env_file, override=True)
    if os.environ.get("POSTGRES_DB") != "aiac_clean":
        raise ValueError("Clean runtime requires its isolated database")
    release=json.loads((args.env_file.parent/'current.json').read_text(encoding='utf-8-sig'))
    if bounded:
        os.environ['AIAC_RECIPE_COMMIT']=release['recipe_commit']
        os.environ['AIAC_HISTORY_EXCLUSIONS']=release['history_exclusions']
        os.environ['AIAC_HISTORY_EXCLUSIONS_SHA256']=release['history_exclusions_sha256']
    if args.service=='worker' and (not bounded or not release.get('worker_enabled')):
        raise ValueError('Bounded worker has not been released')
    if args.service == "ui":
        from aiohttp import web
        from scripts.serve_ui import make_app
        ui_dist=source/'frontend/dist' if bounded else args.env_file.resolve().parent/'ui-dist'
        web.run_app(make_app(ui_dist, release.get('recipe_commit',UPSTREAM_COMMIT) if bounded else UPSTREAM_COMMIT,
                            f"http://127.0.0.1:{args.api_port}"),
                    host="127.0.0.1", port=args.port, access_log=None)
    else:
        os.chdir(source)
        sys.path.insert(0, str(source))
        if args.service=='worker':
            from backend.celery_app import celery_app
            celery_app.worker_main(['worker','--pool=solo','--concurrency=1','--loglevel=INFO',
                                    '--hostname=aiac-clean@%h'])
        else:
            import uvicorn
            uvicorn.run("backend.main:app", host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
