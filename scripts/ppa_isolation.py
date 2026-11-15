"""One-time, CI-bound task 14/run 28 isolation; never sends WQ requests."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTHORIZATION = ('Owner approved 2026-11-15 one-time incomplete-batch isolation for task14/run28; '
                 'preserve unknown requests, freeze replay, deploy only, keep research on hold.')


def private_path(path: Path) -> Path:
    if not path.is_absolute() or path.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError('PPA_ISOLATION_PRIVATE_PATH_REQUIRED')
    return path.resolve()


def write_new(path: Path, payload: dict) -> None:
    path = private_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(payload, stream, indent=2, ensure_ascii=False)
        stream.write('\n')


def evidence_hashes(paths: list[Path]) -> list[str]:
    if not paths or any(not private_path(path).is_file() for path in paths):
        raise ValueError('PPA_ISOLATION_EVIDENCE_REQUIRED')
    return [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]


def require_scope(manifest: dict, commit: str) -> None:
    if (manifest.get('task_id') != 14 or manifest.get('run_id') != 28
            or manifest.get('commit') != commit):
        raise ValueError('PPA_ISOLATION_SCOPE_MISMATCH')


async def execute(action, manifest, commit, hashes):
    from backend.database import AsyncSessionLocal
    from backend.services import ppa_isolation
    async with AsyncSessionLocal() as db:
        if action == 'prepare':
            return await ppa_isolation.prepare(db, 14, 28, commit=commit,
                                              authorization=AUTHORIZATION, evidence_sha256=hashes)
        if action == 'check':
            return await ppa_isolation.inspect(db, manifest)
        return await ppa_isolation.apply(db, manifest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'check', 'apply'))
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--ci-run', type=int, required=True)
    parser.add_argument('--evidence', type=Path, action='append', default=[])
    parser.add_argument('--current-file', type=Path)
    parser.add_argument('--maintenance-file', type=Path)
    parser.add_argument('--journal', type=Path)
    args = parser.parse_args(argv)
    from scripts.verify_release import verify
    ci = verify(args.commit, args.ci_run)
    private_path(args.manifest)
    if not args.env_file.is_absolute():
        raise ValueError('PPA_ISOLATION_ENV_PATH_REQUIRED')
    manifest, hashes, journal = None, [], None
    if args.action == 'prepare':
        if args.manifest.exists():
            raise FileExistsError(args.manifest)
        hashes = evidence_hashes(args.evidence)
    else:
        manifest = json.loads(args.manifest.read_text(encoding='utf-8-sig'))
        require_scope(manifest, args.commit)
    if args.action == 'apply':
        from scripts.ppa_handoff import require_maintenance
        if not args.current_file or not args.maintenance_file or not args.journal:
            raise ValueError('PPA_MAINTENANCE_REQUIRED')
        private_path(args.current_file)
        private_path(args.maintenance_file)
        require_maintenance(args.current_file, args.maintenance_file)
        current = json.loads(args.current_file.read_text(encoding='utf-8-sig'))
        journal = {'outcome': 'PREPARED', 'task_id': 14, 'run_id': 28,
                   'commit': args.commit, 'ci_run': args.ci_run, 'ci_url': ci.get('ci_url'),
                   'previous_commit': current.get('commit'),
                   'manifest_sha256': manifest.get('manifest_sha256'),
                   'at': datetime.now(timezone.utc).isoformat()}
        write_new(args.journal, journal)
    from scripts.launch_runtime import load_runtime_env
    try:
        load_runtime_env(args.env_file)
        result = asyncio.run(execute(args.action, manifest, args.commit, hashes))
    except Exception as exc:
        if journal is not None:
            journal.update(outcome='FAILED_OR_UNCERTAIN', error_type=type(exc).__name__)
            args.journal.write_text(json.dumps(journal, indent=2), encoding='utf-8')
        raise
    if args.action == 'prepare':
        require_scope(result, args.commit)
        write_new(args.manifest, result)
        print(json.dumps({'outcome': 'PREPARED', 'task_id': 14, 'run_id': 28,
                          'manifest_sha256': result.get('manifest_sha256')}))
    else:
        if journal is not None:
            journal.update(outcome='ISOLATED_INCOMPLETE', result=result,
                           completed_at=datetime.now(timezone.utc).isoformat())
            args.journal.write_text(json.dumps(journal, indent=2), encoding='utf-8')
        print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
