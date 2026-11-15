"""Production isolation survives independent lab databases and stale freezes."""
from copy import deepcopy
import importlib

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from backend.models import ExperimentRun, MiningTask, ResearchCandidate
from backend.services import research_audit
from backend.services.ppa_feedback import case_settings, digest
from backend.services.ppa_isolation import isolated_signature
from backend.tests.evals.test_discovery_campaign import campaign_and_batch
from backend.tests.evals.test_live_runner import FakeBrain, clear_state, quality_evaluator, manifest


def helpers():
    return importlib.import_module('backend.evals.isolation_exclusions')


def exclusion_for(case, settings):
    payload = {'version': 1, 'manifests': [{'task_id': 14, 'run_id': 28,
        'manifest_sha256': 'a' * 64,
        'blocked_signatures': [isolated_signature(case['expression'], settings)]}]}
    return {**payload, 'exclusions_sha256': digest(payload)}


def test_missing_or_tampered_export_fails_closed():
    for value in (None, {}, {'version': 1, 'manifests': [], 'exclusions_sha256': 'f' * 64}):
        with pytest.raises(ValueError, match='ISOLATION_EXCLUSIONS'):
            helpers().validate_exclusions(value)


def test_frozen_discovery_rejects_unsent_production_signature():
    from backend.evals.discovery_campaign import freeze_campaign_batch
    campaign, batch = campaign_and_batch()
    evidence = deepcopy(batch['evidence'])
    evidence['isolation_exclusions'] = exclusion_for(batch['cases'][0], case_settings(campaign['base'], {}))
    with pytest.raises(ValueError, match='ISOLATED_SIGNATURE'):
        freeze_campaign_batch(campaign, evidence, {'phase': 'discovery', 'cases': batch['cases']}, index=1)


def test_historical_batch_remains_readable_without_exclusion_export():
    from backend.evals.discovery_campaign import validate_campaign_batch
    campaign, batch = campaign_and_batch()
    batch['evidence'].pop('isolation_exclusions')
    batch['batch_sha256'] = digest({k: v for k, v in batch.items() if k != 'batch_sha256'})
    validate_campaign_batch(campaign, batch)


@pytest.mark.asyncio
async def test_feedback_exports_unsent_frozen_cases_and_rejects_corrupted_record(async_engine, monkeypatch):
    from backend.services import ppa_isolation
    from backend.services.theme_ppa_v2 import feedback_context
    from backend.tests.unit.test_ppa_isolation import fixture, prepare
    from backend.tests.unit.test_ppa_v2_workflow import setup, discovery
    maker, tid, rid, base = await setup(async_engine, monkeypatch)
    await fixture(maker, tid, rid)
    async with maker() as db:
        await ppa_isolation.apply(db, await prepare(db, tid, rid))
    async with maker() as db:
        task, run = await db.get(MiningTask, tid), await db.get(ExperimentRun, rid)
        feedback = await feedback_context(db, task, run)
        exclusions = helpers().validate_exclusions(feedback['isolation_exclusions'])
        assert len(exclusions['manifests']) == 1
        assert helpers().blocked(exclusions, discovery()['cases'][-1]['expression'], case_settings(base, {}))
        assert all('expression' not in record for record in exclusions['manifests'])
        corrupt = deepcopy(run.checkpoint)
        corrupt['ppa_isolation']['blocked_signatures'] = []
        run.checkpoint = corrupt
        await db.commit()
        with pytest.raises(ValueError, match='PPA_ISOLATION_'):
            await feedback_context(db, task, run)


@pytest.mark.asyncio
@pytest.mark.parametrize('runner_kind', ['discovery', 'comparison'])
async def test_changed_live_exclusions_block_before_reservation_in_empty_lab(async_engine, monkeypatch, runner_kind):
    from backend.evals.discovery_campaign import run_campaign_batch
    from backend.evals.live_runner import run_lab
    maker = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(research_audit, 'AsyncSessionLocal', maker)
    brain = FakeBrain({'error': 'must never send'})
    if runner_kind == 'discovery':
        campaign, batch = campaign_and_batch()
        case = batch['cases'][0]
        changed = exclusion_for(case, case_settings(campaign['base'], {}))
        invoke = lambda state: run_campaign_batch(campaign, batch, maker=maker, brain=brain,
            live_state=state, quality_evaluator=quality_evaluator)
    else:
        frozen = manifest()
        changed = exclusion_for(frozen['candidates'][0], frozen['candidates'][0]['settings'])
        invoke = lambda state: run_lab(frozen, maker=maker, brain=brain,
            live_state=state, quality_evaluator=quality_evaluator)
    calls = 0
    def state():
        nonlocal calls
        calls += 1
        return {**clear_state(), **({'isolation_exclusions': changed} if calls > 1 else {})}
    result = await invoke(state)
    assert result['reason'] == 'ISOLATION_EXCLUSIONS_CHANGED'
    assert result['sent'] == brain.posts == 0
    async with maker() as db:
        assert not (await db.execute(select(ResearchCandidate))).scalars().all()


@pytest.mark.asyncio
async def test_old_manifest_can_reconcile_original_but_cannot_send_again(async_engine, monkeypatch):
    from backend.evals.live_runner import run_lab
    maker = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(research_audit, 'AsyncSessionLocal', maker)
    class TrackingBrain(FakeBrain):
        supports_request_tracking = True

        async def simulate_single(self, expression, *, on_location, **kwargs):
            await on_location('/simulations/original')
            return await super().simulate_single(expression, **kwargs)

    brain = TrackingBrain({'error': 'uncertain transport'})
    frozen = manifest()
    first = await run_lab(frozen, maker=maker, brain=brain, live_state=clear_state,
                          quality_evaluator=quality_evaluator)
    assert first['sent'] == brain.posts == 1
    # Keep the ledger binding and remove only old-format metadata from a loaded
    # snapshot, then bind its corresponding historical ledger identity.
    frozen.pop('isolation_exclusions')
    frozen['manifest_sha256'] = digest({k: v for k, v in frozen.items() if k != 'manifest_sha256'})
    async with maker() as db:
        run = await db.get(ExperimentRun, first['run_id'])
        run.checkpoint = {**run.checkpoint, 'manifest_sha256': frozen['manifest_sha256']}
        await db.commit()
    recovered = await run_lab(frozen, maker=maker, brain=brain,
                              live_state=lambda: {**clear_state(), 'isolation_exclusions': None},
                              quality_evaluator=quality_evaluator)
    assert recovered['reason'] == 'EVAL_REQUEST_RECONCILIATION_PENDING'
    assert brain.polls == 1 and brain.posts == 1
