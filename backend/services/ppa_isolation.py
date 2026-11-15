"""Audited isolation preserves incomplete research without claiming reconciliation."""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import re

from sqlalchemy import select, text

from backend.models import ExperimentRun, MiningTask, ResearchCandidate

MANIFEST_FIELDS = frozenset({'version', 'task_id', 'run_id', 'commit', 'authorization',
                            'evidence_sha256', 'state_sha256', 'correction_ids',
                            'blocked_signatures', 'unrelated_pending', 'manifest_sha256'})


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def isolated_signature(expression, settings):
    from backend.services.research_audit import wq_settings_identity
    return {'expression_hash': digest(''.join(expression.lower().split())),
            'settings_hash': digest(wq_settings_identity(settings))}


def _snapshot(obj):
    return {column.name: deepcopy(getattr(obj, column.name)) for column in obj.__table__.columns}


async def _load(db, task_id, run_id):
    tasks = (await db.execute(select(MiningTask).order_by(MiningTask.id).with_for_update()
                             .execution_options(populate_existing=True))).scalars().all()
    task = next((t for t in tasks if t.id == task_id), None)
    runs = (await db.execute(select(ExperimentRun).order_by(ExperimentRun.id).with_for_update()
                            .execution_options(populate_existing=True))).scalars().all()
    own = [r for r in runs if r.task_id == task_id]
    run = own[-1] if own else None
    if not task or not run or run.id != run_id or (task.config or {}).get('research_protocol') != 'theme_ppa_v3':
        raise ValueError('PPA_ISOLATION_SCOPE_CHANGED')
    if any(t.status in {'RUNNING', 'WAITING_LLM'} for t in tasks) or any(
            r.status in {'RUNNING', 'WAITING_LLM', 'PENDING', 'QUEUED'} for r in runs):
        raise ValueError('PPA_ISOLATION_ACTIVE_WORK')
    rows = (await db.execute(select(ResearchCandidate).where(ResearchCandidate.run_id == run_id)
                            .order_by(ResearchCandidate.id).with_for_update()
                            .execution_options(populate_existing=True))).scalars().all()
    if any(row.request_status in {'RESERVED', 'REQUESTED'} for row in rows):
        raise ValueError('PPA_ISOLATION_IN_FLIGHT')
    # Paused unrelated owners are never resumed by a software release. Retain
    # their uncertainty verbatim, while binding it into this exact handoff.
    outstanding = (await db.execute(select(ResearchCandidate).where(
        ResearchCandidate.run_id != run_id,
        ResearchCandidate.request_status.in_(['RESERVED', 'REQUESTED', 'UNKNOWN']))
        .order_by(ResearchCandidate.id).with_for_update()
        .execution_options(populate_existing=True))).scalars().all()
    tasks_by_id, runs_by_id = {t.id: t for t in tasks}, {r.id: r for r in runs}
    unrelated = []
    for row in outstanding:
        owner, owner_run = tasks_by_id.get(row.task_id), runs_by_id.get(row.run_id)
        if (not owner or not owner_run or owner_run.task_id != row.task_id
                or row.task_id == task_id or owner.status not in {'PAUSED', 'STOPPED'}
                or owner_run.status not in {'PAUSED', 'STOPPED'}):
            raise ValueError('PPA_ISOLATION_UNRELATED_PENDING_OWNER')
        unrelated.append({'candidate_id': row.id, 'task_id': owner.id, 'run_id': owner_run.id,
                          'request_status': row.request_status, 'task_status': owner.status,
                          'run_status': owner_run.status, 'candidate_sha256': digest(_snapshot(row)),
                          'task_sha256': digest(_snapshot(owner)), 'run_sha256': digest(_snapshot(owner_run))})
    return task, run, rows, unrelated


def _state(task, run, rows):
    return digest({'task': _snapshot(task), 'run': _snapshot(run), 'candidates': [_snapshot(r) for r in rows]})


def _definitive_failure(snapshot):
    phase = snapshot.get('request_phase')
    if phase == 'POLL_ORIGINAL' and str(snapshot.get('status') or '').upper() in {'ERROR', 'FAILED'}:
        return True
    status = snapshot.get('http_status')
    return phase == 'CREATE_POST' and type(status) is int and 400 <= status < 500 and status != 408


def _plan(task, run, rows):
    from backend.services.ppa_feedback import case_settings
    if task.status != 'PAUSED' or run.status != 'PAUSED':
        raise ValueError('PPA_ISOLATION_REQUIRES_PAUSED')
    cp = run.checkpoint or {}
    cases = (cp.get('ppa_batch') or {}).get('cases') or []
    cursor = cp.get('ppa_batch_cursor')
    if (not cases or type(cursor) is not int or not 0 <= cursor < len(cases)
            or len({c.get('case_id') for c in cases}) != len(cases)
            or any(not c.get('case_id') or not c.get('expression') for c in cases)):
        raise ValueError('PPA_ISOLATION_BATCH_INVALID')
    if cp.get('ppa_isolation'):
        raise ValueError('PPA_ISOLATION_ALREADY_ISOLATED')
    base = (task.config or {}).get('ppa_manifest') or {}
    blocked = []
    for case in cases:
        # Match both serial and parallel runtime dispatch, where task market
        # overrides manifest/case markets and manifest delay is authoritative.
        settings = case_settings({**base, 'region': task.region, 'universe': task.universe},
                                 case.get('settings') or {})
        blocked.append(isolated_signature(case['expression'], settings))
    blocked.extend(isolated_signature(r.expression, r.settings or {}) for r in rows)
    blocked = sorted({json.dumps(item, sort_keys=True): item for item in blocked}.values(),
                     key=lambda x: (x['expression_hash'], x['settings_hash']))
    correction = [r.id for r in rows if r.request_status == 'FAILED' and not r.wq_alpha_id
                  and not _definitive_failure(r.result_snapshot or {})]
    return correction, blocked


async def prepare(db, task_id, run_id, *, commit, authorization, evidence_sha256):
    if (not re.fullmatch('[0-9a-f]{40}', commit or '') or not isinstance(authorization, str)
            or not authorization.strip() or not isinstance(evidence_sha256, list) or not evidence_sha256
            or any(not isinstance(h, str) or not re.fullmatch('[0-9a-f]{64}', h) for h in evidence_sha256)):
        raise ValueError('PPA_ISOLATION_AUTHORIZATION_INVALID')
    task, run, rows, unrelated = await _load(db, task_id, run_id)
    correction, blocked = _plan(task, run, rows)
    manifest = {'version': 1, 'task_id': task_id, 'run_id': run_id, 'commit': commit,
                'authorization': authorization, 'evidence_sha256': evidence_sha256,
                'state_sha256': _state(task, run, rows), 'correction_ids': correction,
                'blocked_signatures': blocked, 'unrelated_pending': unrelated}
    return {**manifest, 'manifest_sha256': digest(manifest)}


def validate_manifest(manifest):
    if (not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS or manifest.get('version') != 1
            or manifest.get('manifest_sha256') != digest({k: v for k, v in manifest.items() if k != 'manifest_sha256'})):
        raise ValueError('PPA_ISOLATION_MANIFEST_INVALID')
    return manifest


def validate_record(record):
    """Validate the embedded immutable manifest before using its exclusions."""
    if (not isinstance(record, dict) or record.get('batch_complete') is not False
            or not isinstance(record.get('isolated_at'), str) or not record['isolated_at']):
        raise ValueError('PPA_ISOLATION_RECORD_INVALID')
    return validate_manifest({key: value for key, value in record.items() if key in MANIFEST_FIELDS})


async def _inspect(db, manifest):
    validate_manifest(manifest)
    task, run, rows, unrelated = await _load(db, manifest['task_id'], manifest['run_id'])
    if unrelated != manifest['unrelated_pending']:
        raise ValueError('PPA_ISOLATION_UNRELATED_PENDING_CHANGED')
    old = (run.checkpoint or {}).get('ppa_isolation') or {}
    if old:
        if (validate_record(old) != manifest or run.status != 'STOPPED'
                or task.status != 'STOPPED' or run.block_reason != 'PPA_HANDOFF_ISOLATED'
                or not (task.config or {}).get('ppa_isolation_hold')):
            raise ValueError('PPA_ISOLATION_STATE_CHANGED')
        if old.get('post_state_sha256') != digest({'task_config': task.config,
                'checkpoint': {k: v for k, v in run.checkpoint.items() if k != 'ppa_isolation'},
                'isolation': {k: v for k, v in old.items() if k != 'post_state_sha256'},
                'candidates': [_snapshot(r) for r in rows]}):
            raise ValueError('PPA_ISOLATION_STATE_CHANGED')
        return task, run, rows, True
    fresh = await prepare(db, task.id, run.id, commit=manifest['commit'],
                          authorization=manifest['authorization'], evidence_sha256=manifest['evidence_sha256'])
    if fresh != manifest:
        raise ValueError('PPA_ISOLATION_STATE_CHANGED')
    return task, run, rows, False


def _summary(manifest):
    return {'outcome': 'ISOLATED_INCOMPLETE', 'task_id': manifest['task_id'], 'run_id': manifest['run_id'],
            'manifest_sha256': manifest['manifest_sha256'], 'corrected_candidate_ids': manifest['correction_ids'],
            'blocked_signature_count': len(manifest['blocked_signatures']),
            'unrelated_pending_candidate_ids': [item['candidate_id'] for item in manifest['unrelated_pending']]}


async def inspect(db, manifest):
    await _inspect(db, manifest)
    return {**_summary(manifest), 'outcome': 'ISOLATION_VALIDATED'}


async def apply(db, manifest):
    # Maintenance stops dispatch; table locks also close insert/update races on PostgreSQL.
    if db.bind.dialect.name == 'postgresql':
        await db.execute(text('LOCK TABLE mining_tasks, experiment_runs, research_candidates IN SHARE ROW EXCLUSIVE MODE'))
    task, run, rows, done = await _inspect(db, manifest)
    if done:
        return _summary(manifest)
    now = datetime.now(timezone.utc)
    for row in rows:
        if row.id in manifest['correction_ids']:
            before = {'request_status': row.request_status, 'result_snapshot': deepcopy(row.result_snapshot)}
            row.result_snapshot = {**(row.result_snapshot or {}), 'isolation_correction': {
                'before': before, 'reason': 'NO_DEFINITIVE_TERMINAL_EVIDENCE', 'at': now.isoformat(),
                'manifest_sha256': manifest['manifest_sha256']}}
            row.request_status = 'UNKNOWN'
    task.config = {**task.config, 'ppa_isolation_hold': {'run_id': run.id,
                  'manifest_sha256': manifest['manifest_sha256']}}
    task.status = run.status = 'STOPPED'
    run.block_reason = 'PPA_HANDOFF_ISOLATED'
    run.next_retry_at = None
    run.finished_at = now.replace(tzinfo=None)
    await db.flush()
    # Server-side updated_at values expire on flush; refresh before hashing so
    # the persisted post-state can be verified from a new async session.
    for row in rows:
        await db.refresh(row)
    record = {**deepcopy(manifest), 'isolated_at': now.isoformat(), 'batch_complete': False}
    record['post_state_sha256'] = digest({'task_config': task.config, 'checkpoint': run.checkpoint,
                                          'isolation': deepcopy(record),
                                          'candidates': [_snapshot(r) for r in rows]})
    run.checkpoint = {**(run.checkpoint or {}), 'ppa_isolation': record}
    await db.commit()
    return _summary(manifest)


async def reservation_block(db, run, task, expression, settings):
    if (run.checkpoint or {}).get('ppa_isolation') or (task.config or {}).get('ppa_isolation_hold'):
        return 'ISOLATED_RUN'
    key = isolated_signature(expression, settings)
    checkpoints = (await db.execute(select(ExperimentRun.checkpoint).where(
        ExperimentRun.block_reason == 'PPA_HANDOFF_ISOLATED'))).scalars().all()
    if any(key in validate_record((cp or {}).get('ppa_isolation'))['blocked_signatures'] for cp in checkpoints):
        return 'ISOLATED_SIGNATURE'
    return None
