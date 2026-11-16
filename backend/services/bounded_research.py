"""Bounded native Celery research. Original Run/Trace/Alpha tables are the ledger.

No simulation retry, no MCP fallback, no LLM polling and no schema replacement.
Uses the original application tables directly; no extra runtime build layer.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlparse

import httpx

BASE = 'https://api.worldquantbrain.com'
MODE = 'bounded'
ACCOUNT_LOCK = 1120261116
PLAN_LOCK = 1120261117


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def normalized(expression):
    return re.sub(r'\s+', '', expression or '').lower()


def freeze(config, region, universe, excluded):
    if config.get('execution_mode') != MODE:
        raise ValueError('Create a bounded research task with frozen candidates first')
    if config.get('simulation_mode', 'single') != 'single':
        raise ValueError('This release supports proven single simulation mode only')
    candidates = config.get('candidates', [])
    budget = config.get('max_create_requests', 4)
    concurrency = config.get('max_concurrent', 1)
    if type(concurrency) is not int or not 1 <= concurrency <= 10:
        raise ValueError('max_concurrent must be 1..10 within the account allowance')
    if type(budget) is not int or not 1 <= budget <= 16 or not 1 <= len(candidates) <= budget:
        raise ValueError('Freeze 1..16 candidates within max_create_requests')
    if not config.get('hypothesis') or not 1 <= len(config.get('fields', [])) <= 2:
        raise ValueError('Economic hypothesis and verified fields are required')
    cases = []
    for i, candidate in enumerate(candidates):
        expression = candidate.get('expression', '').strip()
        if not expression or len(expression) > 4000 or normalized(expression) in excluded:
            raise ValueError('Empty, oversized or historical expression is excluded')
        decay = candidate.get('decay', 4)
        if type(decay) is not int or not 0 <= decay <= 252:
            raise ValueError('Invalid decay')
        settings = dict(instrumentType='EQUITY', region=region, universe=universe,
                        delay=1, decay=decay, neutralization=config.get('neutralization', 'SUBINDUSTRY'),
                        truncation=config.get('truncation', .08), testPeriod='P2Y0M',
                        nanHandling='OFF', unitHandling='VERIFY', pasteurization='ON',
                        language='FASTEXPR', visualization=False, maxTrade='ON')
        if settings['neutralization'] not in ('NONE','MARKET','SECTOR','INDUSTRY','SUBINDUSTRY'):
            raise ValueError('Unsupported neutralization')
        if not isinstance(settings['truncation'], (float,int)) or not .01 <= settings['truncation'] <= .1:
            raise ValueError('Invalid truncation')
        payload = {'type':'REGULAR', 'settings':settings, 'regular':expression}
        cases.append({'case_id':str(candidate.get('id', i + 1)), 'payload':payload, 'signature':digest(payload)})
    if len({x['signature'] for x in cases}) != len(cases) or len({x['case_id'] for x in cases}) != len(cases):
        raise ValueError('Duplicate frozen cases')
    return {'version':1, 'simulation_mode':'single', 'max_create_requests':budget,'max_concurrent':concurrency,
            'family':config.get('family','unspecified'),'continuous':config.get('continuous') is True,
            'hypothesis':config['hypothesis'], 'fields':config['fields'], 'cases':cases}


def simulation_url(url):
    parsed = urlparse(url or '')
    if (parsed.scheme != 'https' or parsed.netloc != 'api.worldquantbrain.com'
            or not re.fullmatch(r'/simulations/[A-Za-z0-9_-]+', parsed.path)
            or parsed.query or parsed.fragment):
        raise ValueError('Invalid simulation Location')
    return url


def alpha_url(alpha_id):
    if not isinstance(alpha_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,20}', alpha_id):
        raise ValueError('Invalid Alpha identity')
    return f'{BASE}/alphas/{alpha_id}'


async def post_once(client, payload):
    receipt = {'started_at':now(), 'request_status':'UNKNOWN'}
    try:
        response = await client.post(BASE + '/simulations', json=payload, follow_redirects=False, timeout=45)
        receipt.update(http_status=response.status_code, received_at=now(),
                       retry_after=response.headers.get('Retry-After'), body=response.text[:8000])
        if response.status_code in (200,201,202) and response.headers.get('Location'):
            receipt.update(location=simulation_url(response.headers['Location']), request_status='ACCEPTED')
        elif response.status_code in (400,401,403,404,422,429):
            receipt['request_status'] = 'REJECTED'
    except Exception as exc:
        # Errors can contain URLs/secrets. Only a safe type reaches the ledger/logs.
        receipt['error_type'] = type(exc).__name__
    return receipt


def quality(alpha):
    """Conservative deterministic gates; absent required evidence never becomes PASS."""
    metrics = alpha.get('is') or {}
    checks = metrics.get('checks') or []
    failed = [c.get('name','unnamed_check') for c in checks if c.get('result') == 'FAIL']
    missing = []
    def finite(value):
        return type(value) in (int,float) and math.isfinite(value)
    rn = metrics.get('riskNeutralized') or {}
    recent = next((c.get('value') for c in checks if c.get('name') == 'LOW_2Y_SHARPE'), None)
    tests = [('sharpe',metrics.get('sharpe'),1.58,None), ('fitness',metrics.get('fitness'),1,None),
             ('margin',metrics.get('margin'),.001,None), ('turnover',metrics.get('turnover'),.05,.30),
             ('rn_sharpe',rn.get('sharpe'),1.58,None), ('rn_fitness',rn.get('fitness'),1,None),
             ('two_year_sharpe',recent,1.6,None)]
    for name,value,low,high in tests:
        if not finite(value): missing.append(name)
        elif value < low or (high is not None and value > high): failed.append(name)
    if (alpha.get('settings') or {}).get('region') == 'GLB':
        amer = (metrics.get('glbAmer') or {}).get('sharpe')
        if not finite(amer): missing.append('AMER_sharpe')
        elif amer <= 1: failed.append('AMER_sharpe')
    for name,limit in [('SELF_CORRELATION',.5),('PROD_CORRELATION',.7)]:
        c = next((c for c in checks if c.get('name') == name), {})
        value = c.get('value')
        if c.get('result') != 'PASS' or not finite(value): missing.append(name)
        elif value >= limit: failed.append(name)
    if not checks or any(c.get('result') == 'PENDING' for c in checks): missing.append('platform_checks')
    count = (alpha.get('regular') or {}).get('operatorCount')
    if type(count) is not int or count < 0: missing.append('operator_count')
    elif count > 5: failed.append('operator_count')
    if alpha.get('stage') != 'IS' or alpha.get('status') != 'UNSUBMITTED': missing.append('stage_status')
    return ('FAIL' if failed else 'PENDING' if missing else 'PASS'), sorted(set(failed)), sorted(set(missing))


def exclusions():
    path = os.environ.get('AIAC_HISTORY_EXCLUSIONS')
    expected = os.environ.get('AIAC_HISTORY_EXCLUSIONS_SHA256')
    if not path or not expected:
        raise ValueError('Historical exclusion manifest is not configured')
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('Historical exclusion manifest changed')
    return {normalized(x.get('expression')) for x in json.loads(raw.decode('utf-8-sig'))}


async def start(db, task_id, resume=False, enqueue=None, next_config=None, previous_run_id=None):
    """Atomic run creation plus durable frozen trace positions; shared by UI and CLI."""
    from sqlalchemy import select, text
    from backend.models import MiningTask, ExperimentRun, TraceStep, Alpha
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key':PLAN_LOCK})
    task = (await db.execute(select(MiningTask).where(MiningTask.id==task_id).with_for_update())).scalar_one_or_none()
    if task is None or (task.config or {}).get('execution_mode') != MODE:
        raise ValueError('Only fresh bounded tasks may execute; legacy tasks remain frozen')
    runs = (await db.execute(select(ExperimentRun).where(ExperimentRun.task_id==task_id).order_by(ExperimentRun.id.desc()))).scalars().all()
    if runs and next_config is None:
        run = runs[0]
        if resume and run.status=='COMPLETED' and task.status=='PAUSED' and task.config.get('continuous') is True:
            from backend.tasks.mining_tasks import continue_research
            task.status='RUNNING'; await db.commit()
            queued=continue_research.delay(task.id,run.id)
            return {'task_id':task.id,'run_id':run.id,'celery_task_id':queued.id,'continuation':True}
        if not resume or run.status == 'COMPLETED' or task.status == 'STOPPED':
            raise ValueError('Run already exists; resume the same eligible run explicitly')
        steps = (await db.execute(select(TraceStep).where(TraceStep.run_id==run.id, TraceStep.step_type=='SIMULATE'))).scalars().all()
        if any((s.output_data or {}).get('request_status') not in ('NOT_REQUESTED','ACCEPTED','RESULT') for s in steps):
            raise ValueError('Uncertain or rejected request blocks resume; never replay it')
        if not steps or not run.config_snapshot.get('bounded_manifest'):
            raise ValueError('No bounded recovery manifest')
    else:
        if next_config is not None:
            if not runs or runs[0].id!=previous_run_id or runs[0].status!='COMPLETED' or task.status!='RUNNING' or task.config.get('continuous') is not True:
                raise ValueError('Continuation is stale, stopped or not authorized')
        elif resume or task.status != 'PENDING': raise ValueError('Fresh bounded task must be PENDING')
        excluded = exclusions()
        excluded.update(normalized(x) for x in (await db.execute(select(Alpha.expression))).scalars())
        manifest = freeze(next_config if next_config is not None else task.config, task.region, task.universe, excluded)
        from validator import ExpressionValidator
        validator = ExpressionValidator()
        for case in manifest['cases']:
            result = validator.check_expression(case['payload']['regular'], allowed_fields=manifest['fields']+['NaN'])
            if not result.get('valid'): raise ValueError('Invalid candidate expression: '+str(result.get('errors'))[:500])
            if 'trade_when(' in normalized(case['payload']['regular']): raise ValueError('trade_when is excluded')
        # Check all native bounded runs, including reserved/not-yet-sent signatures.
        existing = (await db.execute(select(TraceStep.input_data).where(TraceStep.step_type=='SIMULATE'))).scalars().all()
        signatures = {x.get('signature') for x in existing if x}
        if any(c['signature'] in signatures for c in manifest['cases']): raise ValueError('Frozen signature replay blocked')
        run = ExperimentRun(task_id=task_id,status='RUNNING',trigger_source='BOUNDED_CELERY',
                            config_snapshot={'bounded_manifest':manifest,'manifest_sha256':digest(manifest),
                                             'code_commit':os.environ.get('AIAC_CODE_COMMIT')},strategy_snapshot={})
        db.add(run); await db.flush()
        for i,case in enumerate(manifest['cases']):
            db.add(TraceStep(task_id=task_id,run_id=run.id,step_type='SIMULATE',step_order=i+1,iteration=1,
                             status='PENDING',input_data=case,output_data={'request_status':'NOT_REQUESTED'}))
    task.status='RUNNING'; run.status='RUNNING'; run.finished_at=None; run.error_message=None
    await db.commit()
    if enqueue is None:
        from backend.tasks import run_mining_task
        enqueue = run_mining_task.delay
    try:
        queued = enqueue(task_id,run.id)
        run.celery_task_id = queued.id
        await db.commit()
    except Exception:
        task.status='PAUSED'; run.status='PAUSED'; run.error_message='QUEUE_UNAVAILABLE: no new simulation POST'
        await db.commit()
        raise ValueError('Queue unavailable; resume this run after restoring the worker') from None
    return {'task_id':task_id,'run_id':run.id,'celery_task_id':run.celery_task_id,'execution_mode':MODE}


async def report(db, run_id):
    from sqlalchemy import select
    from backend.models import ExperimentRun, TraceStep, Alpha
    run = await db.get(ExperimentRun,run_id)
    if run is None: raise ValueError('Run not found')
    steps = (await db.execute(select(TraceStep).where(TraceStep.run_id==run_id,TraceStep.step_type=='SIMULATE').order_by(TraceStep.step_order))).scalars().all()
    alphas = (await db.execute(select(Alpha).where(Alpha.run_id==run_id))).scalars().all()
    review = (await db.execute(select(TraceStep.output_data).where(TraceStep.run_id==run_id,TraceStep.step_type=='REVIEW'))).scalar_one_or_none()
    counts = {}
    for step in steps:
        status = (step.output_data or {}).get('request_status','UNSPECIFIED')
        counts[status]=counts.get(status,0)+1
    manifest = (run.config_snapshot or {}).get('bounded_manifest',{})
    return {'task_id':run.task_id,'run_id':run.id,'status':run.status,'error':run.error_message,'observed_at':now(),
            'code_commit':(run.config_snapshot or {}).get('code_commit'),'review':review,
            'manifest_sha256':(run.config_snapshot or {}).get('manifest_sha256'),
            'max_create_requests':manifest.get('max_create_requests'),
            'max_concurrent':manifest.get('max_concurrent',1),'continuous':manifest.get('continuous',False),
            'sent':sum(bool((s.output_data or {}).get('reserved_at')) for s in steps),
            'requests':counts,'quality_PASS':sum(a.quality_status=='PASS' for a in alphas),
            'PPA_READY':0,'heartbeat':run.strategy_snapshot or {},
            'cases':[{'trace_id':s.id,**s.input_data,**s.output_data} for s in steps],
            'alphas':[{'id':a.id,'alpha_id':a.alpha_id,'quality_status':a.quality_status,'metrics':a.metrics,
                       'is_metrics':a.is_metrics,'settings':a.settings,'checks':a.checks} for a in alphas]}


async def run(task_id, run_id, client=None, poll_interval=5, max_seconds=1800, after_receipt=None, settle=False):
    """Celery owns the wait. A session advisory lock serializes the account."""
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import AsyncSession
    from backend.database import engine
    from backend.models import MiningTask, ExperimentRun, TraceStep, Alpha
    async with engine.connect() as connection:
        locked = await connection.scalar(text('SELECT pg_try_advisory_lock(:key)'),{'key':ACCOUNT_LOCK})
        await connection.commit()
        if not locked: return {'status':'BUSY','run_id':run_id}
        try:
            async with AsyncSession(bind=connection,expire_on_commit=False) as db:
                task = await db.get(MiningTask,task_id); experiment = await db.get(ExperimentRun,run_id)
                if task is None or experiment is None or experiment.task_id!=task_id:
                    raise ValueError('Invalid run identity')
                manifest = (experiment.config_snapshot or {}).get('bounded_manifest')
                if not manifest or digest(manifest)!=experiment.config_snapshot.get('manifest_sha256'):
                    raise ValueError('Missing or changed frozen manifest')
                if not settle and (task.status!='RUNNING' or experiment.status!='RUNNING'): return {'status':experiment.status}
                steps = (await db.execute(select(TraceStep).where(TraceStep.run_id==run_id,TraceStep.step_type=='SIMULATE').order_by(TraceStep.step_order))).scalars().all()
                if len(steps)!=len(manifest['cases']): raise ValueError('Incomplete trace positions')
                if any(s.input_data!=c for s,c in zip(steps,manifest['cases'])): raise ValueError('Frozen trace changed')
                if settle:
                    # Explicit operational collection after a definitive concurrency rejection.
                    # Never reconcile UNKNOWN or legacy runs; never send a simulation POST.
                    if (not manifest.get('continuous') or task.status not in ('PAUSED','STOPPED') or
                        any(s.output_data.get('request_status') not in ('ACCEPTED','RESULT','REJECTED','NOT_REQUESTED','CANCELLED') for s in steps) or
                        not any(s.output_data.get('request_status')=='REJECTED' and s.output_data.get('http_status')==429
                                and 'CONCURRENT_SIMULATION_LIMIT_EXCEEDED' in s.output_data.get('body','') for s in steps)):
                        raise ValueError('Only explicit concurrency-rejected continuous runs can be settled')
                    task.status='STOPPED';experiment.status='STOPPED'
                    for s in steps:
                        if s.output_data.get('request_status')=='NOT_REQUESTED':
                            s.output_data={**s.output_data,'request_status':'CANCELLED','cancelled_at':now(),
                                           'reason':'Unsent candidate cancelled after concurrency rejection'}
                            s.status='CANCELLED'
                    await db.commit()

                async def heartbeat():
                    experiment.strategy_snapshot={'worker_heartbeat':now(),'pid':os.getpid(),'execution_mode':MODE}
                    await db.commit()

                async def pause(reason):
                    await db.refresh(task)
                    if task.status!='STOPPED': task.status='PAUSED'
                    experiment.status=task.status; experiment.error_message=reason
                    await heartbeat()
                    return {'status':experiment.status,'reason':reason,'run_id':run_id}

                await heartbeat()
                for step in steps:
                    if step.output_data.get('request_status')=='REQUESTED':
                        step.output_data={**step.output_data,'request_status':'UNKNOWN','recovery_at':now()}
                        step.status='FAILED'
                        return await pause('UNKNOWN: worker interrupted before durable Location; POST not replayed')
                if client is None:
                    from backend.adapters.brain_adapter import BrainAdapter
                    adapter = BrainAdapter()
                    try:
                        await adapter.__aenter__(); client=adapter.client
                    except Exception as exc:
                        return await pause('AUTH_UNAVAILABLE: '+type(exc).__name__)
                async def fill_window():
                    active=sum(s.output_data.get('request_status')=='ACCEPTED' for s in steps)
                    for step,case in zip(steps,manifest['cases']):
                        if active>=manifest.get('max_concurrent',1): break
                        if step.output_data.get('request_status')!='NOT_REQUESTED': continue
                        await db.refresh(task)
                        if task.status!='RUNNING': return 'Owner intervention preserved'
                        # Recheck durable exclusions immediately before every create request.
                        if normalized(case['payload']['regular']) in exclusions(): return 'Historical replay blocked'
                        sent=sum(bool(s.output_data.get('reserved_at')) for s in steps)
                        if sent >= manifest['max_create_requests']: return 'Request budget exhausted'
                        step.output_data={'request_status':'REQUESTED','reserved_at':now()}; step.status='RUNNING'
                        await heartbeat()  # commit BEFORE crossing the WQ boundary
                        receipt=await post_once(client,case['payload'])
                        step.output_data={**step.output_data,**receipt}; await heartbeat()
                        if after_receipt: await after_receipt(step)
                        if receipt['request_status']!='ACCEPTED': return 'Request '+receipt['request_status']+'; no POST retry'
                        active+=1
                    return None

                started=time.monotonic()
                for step,case in zip(steps,manifest['cases']):
                    if step.output_data.get('request_status')=='RESULT': continue
                    if settle and step.output_data.get('request_status') in ('REJECTED','CANCELLED'):continue
                    reason=None if settle else await fill_window()
                    if reason: return await pause(reason)
                    status=step.output_data.get('request_status')
                    if status!='ACCEPTED': return await pause('Request '+str(status)+'; no POST retry')
                    location=simulation_url(step.output_data['location'])
                    alpha=None
                    while time.monotonic()-started < max_seconds:
                        await db.refresh(task)
                        if not settle and task.status!='RUNNING': return await pause('Owner intervention preserved; known Location retained')
                        try:
                            response=await client.get(location,follow_redirects=False,timeout=30)
                            step.output_data={**step.output_data,'last_poll_at':now(),'last_poll_status':response.status_code}
                            await heartbeat()
                            if response.status_code in (401,403,429): return await pause('POLL_HTTP_'+str(response.status_code)+'; Location retained')
                            if response.status_code!=200:
                                await asyncio.sleep(poll_interval); continue
                            payload=response.json()
                            if payload.get('status') in ('ERROR','FAILED'):
                                step.output_data={**step.output_data,'request_status':'FAILED','terminal_response':payload}; step.status='FAILED'
                                return await pause('WQ terminal simulation failure')
                            if payload.get('alpha'):
                                detail=await client.get(alpha_url(payload['alpha']),follow_redirects=False,timeout=30)
                                if detail.status_code in (401,403,429): return await pause('ALPHA_HTTP_'+str(detail.status_code))
                                if detail.status_code==200:
                                    alpha=detail.json(); break
                            wait=max(poll_interval,min(60,float(response.headers.get('Retry-After') or poll_interval)))
                            await asyncio.sleep(wait)
                        except (httpx.RequestError,ValueError):
                            await heartbeat(); await asyncio.sleep(poll_interval)
                    if alpha is None: return await pause('POLL_TIME_BUDGET: known Location retained for GET-only resume')
                    if normalized((alpha.get('regular') or {}).get('code'))!=normalized(case['payload']['regular']):
                        return await pause('Alpha expression mismatch')
                    for key,value in case['payload']['settings'].items():
                        if (alpha.get('settings') or {}).get(key)!=value: return await pause('Alpha settings mismatch: '+key)
                    existing=(await db.execute(select(Alpha).where(Alpha.alpha_id==alpha['id']))).scalar_one_or_none()
                    if existing is not None: return await pause('Alpha identity already exists; investigate without replay')
                    verdict,failures,missing=quality(alpha); m=alpha.get('is') or {}; settings=case['payload']['settings']
                    a=Alpha(alpha_id=alpha['id'],task_id=task_id,run_id=run_id,trace_step_id=step.id,
                            expression=case['payload']['regular'],expression_hash=hashlib.sha256(case['payload']['regular'].encode()).hexdigest(),
                            region=settings['region'],universe=settings['universe'],delay=1,decay=settings['decay'],
                            neutralization=settings['neutralization'],truncation=settings['truncation'],
                            status=alpha.get('status'),stage=alpha.get('stage'),quality_status=verdict,
                            settings=alpha.get('settings'),checks=m.get('checks'),is_metrics=m,os_metrics=alpha.get('os'),
                            metrics={**m,'raw_wq':alpha,'failures':failures,'missing':missing,'ppa_status':'UNPROVEN'},
                            hypothesis=manifest['hypothesis'],fields_used=manifest['fields'])
                    for key in ('sharpe','fitness','returns','turnover','margin','drawdown'): setattr(a,'is_'+key,m.get(key))
                    db.add(a); await db.flush()
                    step.output_data={**step.output_data,'request_status':'RESULT','alpha_id':alpha['id'],
                                      'aiac_alpha_id':a.id,'quality_status':verdict,'metrics':m,'failures':failures,'missing':missing}
                    step.status='SUCCESS'; await heartbeat()
                await db.refresh(task)
                if settle:
                    experiment.finished_at=datetime.now(timezone.utc).replace(tzinfo=None)
                    experiment.error_message='Concurrency rejection retained; known results collected, unsent cases cancelled'
                    await heartbeat()
                    return await report(db,run_id)
                if task.status in ('PAUSED','STOPPED'):
                    return await pause('Owner intervention preserved after final RESULT')
                passed=sum(s.output_data.get('quality_status')=='PASS' for s in steps)
                continuous=task.config.get('continuous') is True
                task.progress_current=(task.progress_current or 0)+passed if continuous else passed
                task.current_iteration=(task.current_iteration or 0)+1
                task.status='RUNNING' if continuous else 'COMPLETED'; experiment.status='COMPLETED'
                experiment.finished_at=datetime.now(timezone.utc).replace(tzinfo=None)
                db.add(TraceStep(task_id=task_id,run_id=run_id,step_type='EVALUATE',step_order=len(steps)+1,
                                 status='SUCCESS',input_data={'manifest_sha256':digest(manifest)},
                                 output_data={'RESULT':len(steps),'quality_PASS':passed,'PPA_READY':0,
                                              'next_action':'Review persisted results before another hypothesis batch'}))
                await heartbeat()
                return await report(db,run_id)
        finally:
            await connection.rollback()
            await connection.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':ACCOUNT_LOCK})
            await connection.commit()


async def recover():
    """Requeue RUNNING bounded runs on worker startup, never an old/paused run."""
    from sqlalchemy import select
    from backend.database import AsyncSessionLocal
    from backend.models import ExperimentRun, MiningTask
    from backend.tasks import run_mining_task
    async with AsyncSessionLocal() as db:
        pairs=(await db.execute(select(ExperimentRun,MiningTask).join(MiningTask,ExperimentRun.task_id==MiningTask.id)
               .where(ExperimentRun.status=='RUNNING',MiningTask.status=='RUNNING'))).all()
        for experiment,task in pairs:
            if experiment.config_snapshot.get('bounded_manifest'):
                run_mining_task.delay(task.id,experiment.id)


async def interrupted(task_id, run_id, reason):
    from sqlalchemy import select
    from backend.database import AsyncSessionLocal
    from backend.models import MiningTask, ExperimentRun, TraceStep
    async with AsyncSessionLocal() as db:
        task=await db.get(MiningTask,task_id); experiment=await db.get(ExperimentRun,run_id)
        if not task or not experiment or not experiment.config_snapshot.get('bounded_manifest'): return
        if experiment.status=='COMPLETED': return
        for step in (await db.execute(select(TraceStep).where(TraceStep.run_id==run_id))).scalars():
            if (step.output_data or {}).get('request_status')=='REQUESTED':
                step.output_data={**step.output_data,'request_status':'UNKNOWN','interrupted_at':now()}
                step.status='FAILED'
        if task.status!='STOPPED': task.status='PAUSED'
        experiment.status=task.status; experiment.error_message=reason
        await db.commit()


async def execute(task_id, run_id):
    try:
        return await run(task_id,run_id)
    except Exception as exc:
        await interrupted(task_id,run_id,'WORKER_ERROR: '+type(exc).__name__)
        return {'status':'PAUSED','run_id':run_id,'error_type':type(exc).__name__}


async def health():
    import redis.asyncio as redis
    from backend.config import settings
    from backend.celery_app import celery_app
    from backend.database import AsyncSessionLocal
    from backend.models import ExperimentRun
    from sqlalchemy import select
    r=redis.from_url(settings.REDIS_URL)
    try:
        queue=await r.llen('celery')
    finally:
        await r.aclose()
    replies=await asyncio.to_thread(lambda: celery_app.control.inspect(timeout=2).ping() or {})
    recent=[]
    async with AsyncSessionLocal() as db:
        for snapshot in (await db.execute(select(ExperimentRun.strategy_snapshot).where(ExperimentRun.status=='RUNNING'))).scalars():
            try:
                age=(datetime.now(timezone.utc)-datetime.fromisoformat(snapshot['worker_heartbeat'])).total_seconds()
                if 0 <= age < 120: recent.append({'pid':snapshot['pid'],'age_seconds':round(age,1)})
            except (KeyError,TypeError,ValueError): pass
    return {'execution_mode':MODE,'worker_online':bool(replies or recent),'workers':list(replies),
            'worker_state':'idle_or_ready' if replies else 'busy_recent_heartbeat' if recent else 'offline_or_unknown',
            'recent_heartbeats':recent,
            'queue_length':queue,'historical_exclusions':len(exclusions()),'simulation_mode':'single'}
