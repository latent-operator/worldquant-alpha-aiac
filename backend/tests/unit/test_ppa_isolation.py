from copy import deepcopy
import importlib
import importlib.util

import pytest
from sqlalchemy import select

from backend.models import ExperimentRun, MiningTask, ResearchCandidate
from backend.services.research_audit import ResearchLedger, signature
from backend.services.task_service import TaskService
from backend.tests.unit.test_ppa_v2_workflow import setup, discovery


def service():
    assert importlib.util.find_spec('backend.services.ppa_isolation'), 'Audited isolation is missing'
    return importlib.import_module('backend.services.ppa_isolation')


async def fixture(maker, tid, rid):
    async with maker() as db:
        task, run = await db.get(MiningTask, tid), await db.get(ExperimentRun, rid)
        task.config = {**task.config, 'research_protocol': 'theme_ppa_v3'}
        run.block_reason = 'PPA_BATCH_READY'
        run.checkpoint = {'ppa_batch': discovery(), 'ppa_batch_cursor': 1}
        base = task.config['ppa_manifest']
        settings = {**base['settings'], 'region': task.region, 'universe': task.universe, 'delay': base['delay']}
        for index, case in enumerate(discovery()['cases'][:2]):
            eh, sh = signature(case['expression'], settings)
            db.add(ResearchCandidate(task_id=tid, run_id=rid, source='DISCOVERY', family_id='test',
                experiment_group=case['case_id'], expression=case['expression'], expression_hash=eh,
                settings_hash=sh, settings=settings, validation_status='VALID',
                request_status='RESULT' if index == 0 else 'FAILED',
                wq_alpha_id='alpha1' if index == 0 else None,
                metrics={}, gate_failures=[], result_snapshot={'ppa_evaluation': {'status': 'PPA_REJECTED'}}
                if index == 0 else {'success': False, 'request': {'location': '/simulations/original',
                    'sent_at': '2026-11-12T00:00:00Z', 'finished_at': '2026-11-13T00:00:00Z'}}))
        await db.commit()
        return deepcopy(run.checkpoint), settings


async def prepare(db, tid, rid):
    return await service().prepare(db, tid, rid, commit='a'*40,
        authorization='Owner explicitly approved incomplete-batch isolation', evidence_sha256=['b'*64])


@pytest.mark.asyncio
async def test_atomic_isolation_preserves_uncertainty_batch_and_original_evidence(async_engine, monkeypatch):
    maker, tid, rid, _ = await setup(async_engine, monkeypatch)
    original, _ = await fixture(maker, tid, rid)
    async with maker() as db:
        manifest = await prepare(db, tid, rid)
        assert (await db.get(ExperimentRun, rid)).checkpoint == original
        result = await service().apply(db, manifest)
        assert result['outcome'] == 'ISOLATED_INCOMPLETE'
    async with maker() as db:
        task, run = await db.get(MiningTask, tid), await db.get(ExperimentRun, rid)
        assert task.status == run.status == 'STOPPED'
        assert run.block_reason == 'PPA_HANDOFF_ISOLATED'
        assert run.checkpoint['ppa_batch_cursor'] == original['ppa_batch_cursor']
        assert run.checkpoint['ppa_batch'] == original['ppa_batch']
        rows = (await db.execute(select(ResearchCandidate).where(ResearchCandidate.run_id == rid)
                                .order_by(ResearchCandidate.id))).scalars().all()
        assert [r.request_status for r in rows] == ['RESULT', 'UNKNOWN']
        assert rows[1].result_snapshot['request']['location'] == '/simulations/original'
        assert rows[1].result_snapshot['request']['finished_at'] == '2026-11-13T00:00:00Z'
        assert rows[1].result_snapshot['isolation_correction']['before']['request_status'] == 'FAILED'
        assert len(run.checkpoint['ppa_isolation']['blocked_signatures']) == 4
        assert await service().apply(db, manifest) == result


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['cursor', 'candidate', 'new_run', 'active_task', 'manifest'])
async def test_isolation_rejects_changed_evidence_without_mutation(async_engine, monkeypatch, mutation):
    maker, tid, rid, _ = await setup(async_engine, monkeypatch)
    await fixture(maker, tid, rid)
    async with maker() as db:
        manifest = await prepare(db, tid, rid)
        run = await db.get(ExperimentRun, rid)
        if mutation == 'cursor': run.checkpoint = {**run.checkpoint, 'ppa_batch_cursor': 2}
        if mutation == 'candidate':
            row = (await db.execute(select(ResearchCandidate).where(ResearchCandidate.run_id == rid))).scalars().first()
            row.metrics = {'sharpe': 8}
        if mutation == 'new_run': db.add(ExperimentRun(task_id=tid, status='PAUSED'))
        if mutation == 'active_task':
            db.add(MiningTask(task_name='busy', region='USA', universe='TOP3000', status='RUNNING'))
        if mutation == 'manifest': manifest['correction_ids'] = []
        await db.commit()
        with pytest.raises(ValueError, match='PPA_ISOLATION_'):
            await service().apply(db, manifest)
        assert (await db.get(MiningTask, tid)).status == 'PAUSED'


@pytest.mark.asyncio
async def test_isolation_blocks_start_resume_and_replay_in_another_task(async_engine, monkeypatch):
    maker, tid, rid, _ = await setup(async_engine, monkeypatch)
    _, settings = await fixture(maker, tid, rid)
    async with maker() as db:
        manifest = await prepare(db, tid, rid)
        await service().apply(db, manifest)
        for operation in (TaskService(db).start_task(tid), TaskService(db).intervene_task(tid, 'RESUME')):
            with pytest.raises(ValueError, match='PPA_ISOLATION_RESEARCH_HOLD'):
                await operation
        other = MiningTask(task_name='new experiment', region='USA', universe='TOP3000', status='PAUSED')
        db.add(other)
        await db.flush()
        run = ExperimentRun(task_id=other.id, status='PAUSED')
        db.add(run)
        await db.commit()
        oid, orid = other.id, run.id
    for case in discovery()['cases']:
        slot, reason = await ResearchLedger(oid, orid, 80).reserve(case['expression'], settings, 'DISCOVERY')
        assert slot is None and reason == 'ISOLATED_SIGNATURE'
    slot, reason = await ResearchLedger(tid, rid, 80).reserve('rank(new_field)', settings, 'DISCOVERY')
    assert slot is None and reason == 'ISOLATED_RUN'


@pytest.mark.asyncio
@pytest.mark.parametrize('pending_status', ['RESERVED', 'REQUESTED'])
async def test_definitive_failure_is_not_reclassified_and_requested_refused(async_engine, monkeypatch, pending_status):
    maker, tid, rid, _ = await setup(async_engine, monkeypatch)
    await fixture(maker, tid, rid)
    async with maker() as db:
        row = (await db.execute(select(ResearchCandidate).where(ResearchCandidate.run_id == rid,
                                ResearchCandidate.request_status == 'FAILED'))).scalar_one()
        row.result_snapshot = {'http_status': 400, 'request_phase': 'CREATE_POST'}
        await db.commit()
        manifest = await prepare(db, tid, rid)
        assert manifest['correction_ids'] == []
        row.request_status = pending_status
        await db.commit()
        with pytest.raises(ValueError, match='PPA_ISOLATION_'):
            await prepare(db, tid, rid)


@pytest.mark.asyncio
@pytest.mark.parametrize('snapshot,correct', [
    ({'http_status': 404, 'request_phase': 'POLL_ORIGINAL'}, True),
    ({'http_status': 405, 'request_phase': 'POLL_ORIGINAL'}, True),
    ({'http_status': 400}, True),
    ({'http_status': 408, 'request_phase': 'CREATE_POST'}, True),
    ({'http_status': 503, 'request_phase': 'CREATE_POST'}, True),
    ({'http_status': 400, 'request_phase': 'CREATE_POST'}, False),
    ({'status': 'FAILED', 'request_phase': 'POLL_ORIGINAL'}, False),
    ({'status': 'ERROR', 'request_phase': 'POLL_ORIGINAL'}, False),
    ({'status': 'FAILED', 'request_phase': 'CREATE_POST'}, True),
])
async def test_correction_requires_explicit_terminal_evidence(async_engine, monkeypatch, snapshot, correct):
    maker, tid, rid, _ = await setup(async_engine, monkeypatch)
    await fixture(maker, tid, rid)
    async with maker() as db:
        row = (await db.execute(select(ResearchCandidate).where(
            ResearchCandidate.run_id == rid, ResearchCandidate.request_status == 'FAILED'))).scalar_one()
        row.result_snapshot = snapshot
        await db.commit()
        manifest = await prepare(db, tid, rid)
        assert (row.id in manifest['correction_ids']) is correct


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['signatures', 'cursor', 'candidate', 'hold', 'isolation_time'])
async def test_restart_inspect_rejects_modified_isolation_state(async_engine, monkeypatch, mutation):
    maker, tid, rid, _ = await setup(async_engine, monkeypatch)
    await fixture(maker, tid, rid)
    async with maker() as db:
        manifest = await prepare(db, tid, rid)
        await service().apply(db, manifest)
    async with maker() as db:
        assert (await service().inspect(db, manifest))['outcome'] == 'ISOLATION_VALIDATED'
        run = await db.get(ExperimentRun, rid)
        cp = deepcopy(run.checkpoint)
        if mutation == 'signatures': cp['ppa_isolation']['blocked_signatures'] = []
        if mutation == 'isolation_time': cp['ppa_isolation']['isolated_at'] = '2025-01-01T00:00:00Z'
        if mutation == 'cursor': cp['ppa_batch_cursor'] += 1
        run.checkpoint = cp
        if mutation == 'candidate':
            row = (await db.execute(select(ResearchCandidate).where(ResearchCandidate.run_id == rid))).scalars().first()
            row.quality_status = 'PASS'
        if mutation == 'hold':
            task = await db.get(MiningTask, tid)
            task.config = {**task.config, 'ppa_isolation_hold': None}
        await db.commit()
    async with maker() as db:
        with pytest.raises(ValueError, match='PPA_ISOLATION_'):
            await service().inspect(db, manifest)


@pytest.mark.asyncio
async def test_frozen_signatures_follow_actual_dispatch_settings(async_engine, monkeypatch):
    maker, tid, rid, _ = await setup(async_engine, monkeypatch)
    await fixture(maker, tid, rid)
    async with maker() as db:
        task, run = await db.get(MiningTask, tid), await db.get(ExperimentRun, rid)
        base = deepcopy(task.config['ppa_manifest'])
        base.update(region='GLB', universe='TOP1000', delay=0)
        task.config = {**task.config, 'ppa_manifest': base}
        cp = deepcopy(run.checkpoint)
        cp['ppa_batch']['cases'][3]['settings'] = {'decay': 9, 'region': 'CHN', 'delay': 1}
        run.checkpoint = cp
        await db.commit()
        manifest = await prepare(db, tid, rid)
        case = cp['ppa_batch']['cases'][3]
        dispatch = {**base['settings'], **case['settings'], 'region': task.region,
                    'universe': task.universe, 'delay': base['delay']}
        assert service().isolated_signature(case['expression'], dispatch) in manifest['blocked_signatures']
        equivalent = {**dispatch, 'testPeriod': dispatch.get('test_period', 'P2Y0M')}
        equivalent.pop('test_period', None)
        assert service().isolated_signature('  ' + case['expression'].upper() + ' ', equivalent) in manifest['blocked_signatures']


@pytest.mark.asyncio
async def test_task_adjust_cannot_remove_isolation_and_stale_resume_refreshes_hold(async_engine, monkeypatch):
    maker, tid, rid, _ = await setup(async_engine, monkeypatch)
    await fixture(maker, tid, rid)
    async with maker() as stale:
        cached = await stale.get(MiningTask, tid)
        assert cached.status == 'PAUSED'
        async with maker() as operator:
            manifest = await prepare(operator, tid, rid)
            await service().apply(operator, manifest)
        with pytest.raises(ValueError, match='PPA_ISOLATION_RESEARCH_HOLD'):
            await TaskService(stale).intervene_task(tid, 'RESUME', {'expected_run_id': rid})
    async with maker() as db:
        with pytest.raises(ValueError, match='PPA_ISOLATION_RESEARCH_HOLD'):
            await TaskService(db).intervene_task(tid, 'ADJUST', {'ppa_isolation_hold': None})
        assert (await db.get(MiningTask, tid)).config['ppa_isolation_hold']


@pytest.mark.asyncio
async def test_restart_and_release_rollback_never_requeue_isolated_run(async_engine, monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from backend import database
    from backend.celery_app import _recover_unfinished_runs, celery_app
    from scripts import release_tasks

    maker, tid, rid, _ = await setup(async_engine, monkeypatch)
    await fixture(maker, tid, rid)
    before = {'tasks': [{'task_id': tid, 'run_id': rid, 'task_status': 'PAUSED'}]}
    async with maker() as db:
        manifest = await prepare(db, tid, rid)
        await service().apply(db, manifest)
    monkeypatch.setattr(database, 'AsyncSessionLocal', maker)
    send = Mock(side_effect=AssertionError('Isolated run must never be queued'))
    monkeypatch.setattr(celery_app, 'send_task', send)
    assert await _recover_unfinished_runs() == 0
    send.assert_not_called()

    async def current():
        async with maker() as db:
            task = await db.get(MiningTask, tid)
            return {'tasks': [{'task_id': tid, 'run_id': rid, 'task_status': task.status}]}
    monkeypatch.setattr(release_tasks, 'capture', current)
    resume = AsyncMock(side_effect=AssertionError('Rollback must preserve research hold'))
    monkeypatch.setattr(TaskService, 'intervene_task', resume)
    await release_tasks.resume_original(before)
    resume.assert_not_called()
    async with maker() as db:
        assert (await db.get(ExperimentRun, rid)).status == 'STOPPED'
        assert (await db.get(MiningTask, tid)).config['ppa_isolation_hold']


async def unrelated_pending(maker, *, status='REQUESTED', owner_status='PAUSED'):
    async with maker() as db:
        task = MiningTask(task_name='unrelated old task', region='USA', universe='TOP3000',
                          status=owner_status, config={'historical': True})
        db.add(task)
        await db.flush()
        run = ExperimentRun(task_id=task.id, status=owner_status, checkpoint={'original': True})
        db.add(run)
        await db.flush()
        expression = 'rank(unrelated_signal)'
        eh, sh = signature(expression, {})
        row = ResearchCandidate(task_id=task.id, run_id=run.id, source='DISCOVERY',
            family_id='unrelated', expression=expression, expression_hash=eh, settings_hash=sh,
            settings={}, validation_status='VALID', request_status=status,
            result_snapshot={'request': {'sent_at': '2026-10-26T00:00:00Z'}})
        db.add(row)
        await db.commit()
        return task.id, run.id, row.id


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['RESERVED', 'REQUESTED', 'UNKNOWN'])
@pytest.mark.parametrize('owner_status', ['PAUSED', 'STOPPED'])
async def test_unrelated_inactive_pending_is_bound_and_preserved(async_engine, monkeypatch, status, owner_status):
    maker, tid, rid, _ = await setup(async_engine, monkeypatch)
    await fixture(maker, tid, rid)
    otid, orid, cid = await unrelated_pending(maker, status=status, owner_status=owner_status)
    async with maker() as db:
        objects = [await db.get(MiningTask, otid), await db.get(ExperimentRun, orid),
                   await db.get(ResearchCandidate, cid)]
        before = [service()._snapshot(obj) for obj in objects]
        manifest = await prepare(db, tid, rid)
        assert manifest['unrelated_pending'] == [{
            'candidate_id': cid, 'task_id': otid, 'run_id': orid, 'request_status': status,
            'task_status': owner_status, 'run_status': owner_status,
            'task_sha256': service().digest(before[0]), 'run_sha256': service().digest(before[1]),
            'candidate_sha256': service().digest(before[2])}]
        result = await service().apply(db, manifest)
        assert result['unrelated_pending_candidate_ids'] == [cid]
    async with maker() as db:
        await service().inspect(db, manifest)
        objects = [await db.get(MiningTask, otid), await db.get(ExperimentRun, orid),
                   await db.get(ResearchCandidate, cid)]
        assert [service()._snapshot(obj) for obj in objects] == before


@pytest.mark.asyncio
@pytest.mark.parametrize('after_apply', [False, True])
@pytest.mark.parametrize('mutation', ['candidate', 'task', 'run', 'resolved', 'new_pending'])
async def test_unrelated_pending_change_invalidates_manifest(async_engine, monkeypatch, after_apply, mutation):
    maker, tid, rid, _ = await setup(async_engine, monkeypatch)
    await fixture(maker, tid, rid)
    otid, orid, cid = await unrelated_pending(maker)
    async with maker() as db:
        manifest = await prepare(db, tid, rid)
        if after_apply:
            await service().apply(db, manifest)
    async with maker() as db:
        if mutation == 'candidate':
            (await db.get(ResearchCandidate, cid)).result_snapshot = {'request': {'location': '/simulations/changed'}}
        if mutation == 'task': (await db.get(MiningTask, otid)).config = {'changed': True}
        if mutation == 'run': (await db.get(ExperimentRun, orid)).checkpoint = {'changed': True}
        if mutation == 'resolved': (await db.get(ResearchCandidate, cid)).request_status = 'FAILED'
        await db.commit()
    if mutation == 'new_pending': await unrelated_pending(maker, status='UNKNOWN')
    async with maker() as db:
        with pytest.raises(ValueError, match='PPA_ISOLATION_'):
            await service().apply(db, manifest)
        assert (await db.get(MiningTask, tid)).status == ('STOPPED' if after_apply else 'PAUSED')


@pytest.mark.asyncio
@pytest.mark.parametrize('owner_status', ['RUNNING', 'WAITING_LLM', 'COMPLETED'])
async def test_unrelated_pending_requires_inactive_paused_or_stopped_owner(async_engine, monkeypatch, owner_status):
    maker, tid, rid, _ = await setup(async_engine, monkeypatch)
    await fixture(maker, tid, rid)
    await unrelated_pending(maker, owner_status=owner_status)
    async with maker() as db:
        with pytest.raises(ValueError, match='PPA_ISOLATION_'):
            await prepare(db, tid, rid)


@pytest.mark.asyncio
async def test_unrelated_pending_rechecked_from_persisted_state_in_cached_session(async_engine, monkeypatch):
    maker, tid, rid, _ = await setup(async_engine, monkeypatch)
    await fixture(maker, tid, rid)
    _, _, cid = await unrelated_pending(maker)
    async with maker() as cached:
        original = await cached.get(ResearchCandidate, cid)
        manifest = await prepare(cached, tid, rid)
        async with maker() as changed:
            row = await changed.get(ResearchCandidate, cid)
            row.result_snapshot = {'request': {'location': '/simulations/new-evidence'}}
            await changed.commit()
        assert original.result_snapshot != row.result_snapshot
        with pytest.raises(ValueError, match='PPA_ISOLATION_UNRELATED_PENDING_CHANGED'):
            await service().apply(cached, manifest)


@pytest.mark.asyncio
async def test_pending_on_other_run_of_target_task_is_not_unrelated(async_engine, monkeypatch):
    maker, tid, rid, _ = await setup(async_engine, monkeypatch)
    await fixture(maker, tid, rid)
    _, _, cid = await unrelated_pending(maker)
    async with maker() as db:
        # Put the pending request on an older run of the target task. Shared
        # owner changes would invalidate unrelated-owner preservation.
        root = (await db.execute(select(ExperimentRun).where(ExperimentRun.id < rid)
                                .order_by(ExperimentRun.id))).scalars().first()
        root.task_id = tid
        row = await db.get(ResearchCandidate, cid)
        row.task_id, row.run_id = tid, root.id
        await db.commit()
        with pytest.raises(ValueError, match='PPA_ISOLATION_UNRELATED_PENDING_OWNER'):
            await prepare(db, tid, rid)
