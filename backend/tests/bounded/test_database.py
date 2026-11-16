"""Offline PostgreSQL execution/recovery tests, only in an explicitly named test DB."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

import httpx

SOURCE = os.environ.get('AIAC_BOUNDED_TEST_SOURCE')
if SOURCE:
    sys.path.insert(0, SOURCE)


@unittest.skipUnless(SOURCE, 'Set AIAC_BOUNDED_TEST_SOURCE for isolated integration tests')
class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from backend.config import settings
        self.assertTrue(settings.POSTGRES_DB.startswith('aiac_bounded_test'), 'Never reset a production DB')
        from backend.database import SQLAlchemyBase, engine, AsyncSessionLocal
        import backend.models
        from backend.services import bounded_research
        self.b=bounded_research; self.sessions=AsyncSessionLocal
        async with engine.begin() as conn:
            await conn.run_sync(SQLAlchemyBase.metadata.drop_all)
            await conn.run_sync(SQLAlchemyBase.metadata.create_all)
        self.tmp=tempfile.TemporaryDirectory()
        p=Path(self.tmp.name)/'exclusions.json'; p.write_text('[]')
        self.env=patch.dict(os.environ,{'AIAC_HISTORY_EXCLUSIONS':str(p),
                                      'AIAC_HISTORY_EXCLUSIONS_SHA256':hashlib.sha256(b'[]').hexdigest()})
        self.env.start(); self.calls=[]; self.payload=None

    async def asyncTearDown(self):
        self.env.stop(); self.tmp.cleanup()

    async def create(self, candidates=None):
        from backend.models import MiningTask
        candidates=candidates or [{'id':'a','expression':'rank(close)','decay':4}]
        async with self.sessions() as db:
            task=MiningTask(task_name='offline',region='USA',universe='TOP3000',status='PENDING',
                config={'execution_mode':'bounded','hypothesis':'offline test only','fields':['close'],
                        'max_create_requests':len(candidates),'candidates':candidates})
            db.add(task); await db.commit(); task_id=task.id
            result=await self.b.start(db,task_id,enqueue=lambda *a: type('Job',(),{'id':'offline-job'})())
            return task_id,result['run_id']

    def transport(self, request):
        self.calls.append(request.method+' '+request.url.path)
        if request.method=='POST':
            self.payload=json.loads(request.content)
            return httpx.Response(201,headers={'Location':self.b.BASE+'/simulations/offline'})
        if request.url.path.startswith('/simulations/'):
            return httpx.Response(200,json={'status':'COMPLETE','alpha':'offlineAlpha'})
        return httpx.Response(200,json={'id':'offlineAlpha','regular':{'code':self.payload['regular'],'operatorCount':1},
            'settings':self.payload['settings'],'status':'UNSUBMITTED','stage':'IS',
            'is':{'sharpe':.2,'fitness':.1,'turnover':.1,'margin':.0001,'checks':[{'name':'LOW_SHARPE','result':'FAIL'}]}})

    async def test_result_persists_and_duplicate_delivery_never_posts_again(self):
        task_id,run_id=await self.create()
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.transport)) as client:
            result=await self.b.run(task_id,run_id,client=client,poll_interval=0)
            again=await self.b.run(task_id,run_id,client=client,poll_interval=0)
        self.assertEqual(result['requests'],{'RESULT':1}); self.assertEqual(result['quality_PASS'],0)
        self.assertEqual(again['status'],'COMPLETED')
        self.assertEqual(sum(x.startswith('POST') for x in self.calls),1)

    async def test_completion_review_is_once_and_never_changes_quality(self):
        from backend.services import completion_review as c
        from backend.models import TraceStep
        from sqlalchemy import select
        task_id,run_id=await self.create()
        analysis={'decision':'CHANGE_MECHANISM','summary':'Weak evidence','next_action':'New hypothesis required'}
        with patch.object(c,'analyze',AsyncMock(return_value=analysis)) as llm, patch.object(c,'desktop_notify',AsyncMock(return_value='SHOWN')) as notify:
            self.assertIsNone(await c.complete(run_id))  # still RUNNING
            llm.assert_not_awaited()
            async with httpx.AsyncClient(transport=httpx.MockTransport(self.transport)) as client:
                await self.b.run(task_id,run_id,client=client,poll_interval=0)
            first=await c.complete(run_id); again=await c.complete(run_id)
            self.assertEqual(first['facts']['quality_PASS'],0)
            self.assertEqual(first['facts']['cases'][0]['alpha_id'],'offlineAlpha')
            self.assertEqual(again['desktop_status'],'SHOWN')
            llm.assert_awaited_once(); notify.assert_awaited_once()
        async with self.sessions() as db:
            reviews=(await db.execute(select(TraceStep).where(TraceStep.run_id==run_id,TraceStep.step_type=='REVIEW'))).scalars().all()
            self.assertEqual(len(reviews),1)
            self.assertEqual((await self.b.report(db,run_id))['quality_PASS'],0)

    async def test_review_failure_keeps_completion_and_durable_notification(self):
        from backend.services import completion_review as c
        task_id,run_id=await self.create()
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.transport)) as client:
            await self.b.run(task_id,run_id,client=client,poll_interval=0)
        with patch.object(c,'analyze',AsyncMock(side_effect=TimeoutError)), patch.object(c,'desktop_notify',AsyncMock(side_effect=OSError)):
            result=await c.complete(run_id)
        self.assertEqual(result['analysis_status'],'FAILED')
        self.assertEqual(result['desktop_status'],'FAILED')
        with patch.object(c,'analyze',AsyncMock()) as llm, patch.object(c,'desktop_notify',AsyncMock(return_value='SUBMITTED')):
            retried=await c.complete(run_id,retry_desktop=True)
            self.assertEqual(retried['desktop_status'],'SUBMITTED')
            self.assertEqual(retried['previous_desktop_status'],'FAILED')
            llm.assert_not_awaited()
        async with self.sessions() as db:
            self.assertEqual((await self.b.report(db,run_id))['status'],'COMPLETED')
            self.assertEqual((await c.notifications(db))[0]['run_id'],run_id)

    async def test_uncertain_desktop_delivery_is_not_repeated(self):
        from backend.services import completion_review as c
        from backend.models import TraceStep
        from sqlalchemy import select
        task_id,run_id=await self.create()
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.transport)) as client:
            await self.b.run(task_id,run_id,client=client,poll_interval=0)
        with patch.object(c,'analyze',AsyncMock(return_value={'decision':'STOP','summary':'weak','next_action':'stop'})), patch.object(c,'desktop_notify',AsyncMock(return_value='SHOWN')):
            await c.complete(run_id)
        async with self.sessions() as db:
            step=(await db.execute(select(TraceStep).where(TraceStep.run_id==run_id,TraceStep.step_type=='REVIEW'))).scalar_one()
            step.output_data={**step.output_data,'desktop_status':'ATTEMPTED'}; await db.commit()
        with patch.object(c,'desktop_notify',AsyncMock()) as notify:
            result=await c.complete(run_id)
            notify.assert_not_awaited(); self.assertEqual(result['desktop_status'],'ATTEMPTED')

    async def test_restart_recovers_completed_review_without_mining(self):
        from backend.services import completion_review as c
        task_id,run_id=await self.create()
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.transport)) as client:
            await self.b.run(task_id,run_id,client=client,poll_interval=0)
        with patch('backend.tasks.mining_tasks.review_completed_run.delay') as review, patch('backend.tasks.run_mining_task.delay') as mining:
            await c.recover()
            review.assert_called_once_with(run_id); mining.assert_not_called()

    async def test_native_completion_hook_survives_review_failure(self):
        from backend.tasks.mining_tasks import run_mining_task
        from backend.services import completion_review as c
        with patch.object(self.b,'execute',AsyncMock(return_value={'status':'COMPLETED','run_id':42})), patch.object(c,'complete',AsyncMock(side_effect=RuntimeError)) as review:
            result=await asyncio.to_thread(run_mining_task.run,8,42)
            self.assertEqual(result['status'],'COMPLETED'); review.assert_awaited_once_with(42)

    async def test_parallel_window_sends_ten_before_polling(self):
        from backend.models import MiningTask
        candidates=[{'id':str(i),'expression':f'ts_mean(close, {i+2})'} for i in range(10)]
        task_id,run_id=await self.create(candidates)
        # Recreate the frozen manifest to exercise the public configuration path.
        from backend.models import ExperimentRun,TraceStep
        from sqlalchemy import select
        async with self.sessions() as db:
            task=await db.get(MiningTask,task_id)
            task.config={**task.config,'max_concurrent':10}
            manifest=self.b.freeze(task.config,task.region,task.universe,set())
            run=await db.get(ExperimentRun,run_id)
            run.config_snapshot={**run.config_snapshot,'bounded_manifest':manifest,'manifest_sha256':self.b.digest(manifest)}
            await db.commit()
        payloads={}; calls=[]
        def transport(request):
            calls.append(request.method)
            if request.method=='POST':
                key=str(len(payloads));payloads[key]=json.loads(request.content)
                return httpx.Response(201,headers={'Location':self.b.BASE+'/simulations/'+key})
            key=request.url.path.rsplit('/',1)[-1]
            if '/simulations/' in request.url.path:return httpx.Response(200,json={'alpha':key})
            return httpx.Response(200,json={'id':key,'regular':{'code':payloads[key]['regular'],'operatorCount':1},'settings':payloads[key]['settings'],'stage':'IS','status':'UNSUBMITTED','is':{}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            result=await self.b.run(task_id,run_id,client=client,poll_interval=0)
        self.assertEqual(calls[:10],['POST']*10)
        self.assertEqual(calls.count('POST'),10)
        self.assertEqual(result['requests'],{'RESULT':10})

    async def test_continuation_creates_one_run_and_preserves_stop(self):
        from backend.services import continuous_research as c
        from backend.models import MiningTask,TraceStep,ExperimentRun
        from sqlalchemy import select
        task_id,run_id=await self.create()
        async with self.sessions() as db:
            task=await db.get(MiningTask,task_id)
            task.config={**task.config,'continuous':True,'verified_fields':['close'],'max_concurrent':1}
            await db.commit()
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.transport)) as client:
            await self.b.run(task_id,run_id,client=client,poll_interval=0)
        async with self.sessions() as db:
            self.assertEqual((await db.get(MiningTask,task_id)).status,'RUNNING')
            db.add(TraceStep(task_id=task_id,run_id=run_id,step_type='REVIEW',step_order=3,status='SUCCESS',output_data={'analysis_status':'COMPLETED','analysis':{'decision':'STOP'}}));await db.commit()
        plan={'hypothesis':'new mechanism','family':'new-family','fields':['close'],'candidates':[{'id':'next','expression':'ts_mean(close, 12)'}]}
        with patch.object(c,'propose',AsyncMock(return_value=plan)), patch.object(c,'verify_fields',AsyncMock()), patch('backend.tasks.run_mining_task.delay',return_value=type('Job',(),{'id':'offline-next'})()) as queued:
            await c.advance(task_id,run_id)
            await c.advance(task_id,run_id)
            self.assertEqual(queued.call_count,1)
        async with self.sessions() as db:
            runs=(await db.execute(select(ExperimentRun).where(ExperimentRun.task_id==task_id))).scalars().all()
            self.assertEqual(len(runs),2)
            self.assertEqual(runs[0].status,'COMPLETED')
            task=await db.get(MiningTask,task_id);task.status='STOPPED';await db.commit()
        with patch.object(c,'propose',AsyncMock()) as planner:
            await c.advance(task_id,runs[-1].id);planner.assert_not_awaited()

    async def test_parallel_429_preserves_accepted_receipts_and_stops_posting(self):
        from backend.models import MiningTask,ExperimentRun
        task_id,run_id=await self.create([{'id':str(i),'expression':f'ts_mean(close,{i+2})'} for i in range(4)])
        async with self.sessions() as db:
            task=await db.get(MiningTask,task_id)
            manifest=self.b.freeze({**task.config,'max_concurrent':4},task.region,task.universe,set())
            run=await db.get(ExperimentRun,run_id)
            run.config_snapshot={**run.config_snapshot,'bounded_manifest':manifest,'manifest_sha256':self.b.digest(manifest)};await db.commit()
        calls=[]
        def transport(request):
            calls.append(request.method)
            if len(calls)==3:return httpx.Response(429)
            return httpx.Response(201,headers={'Location':self.b.BASE+'/simulations/'+str(len(calls))})
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await self.b.run(task_id,run_id,client=client,poll_interval=0)
        self.assertEqual(calls,['POST']*3)
        async with self.sessions() as db:
            result=await self.b.report(db,run_id)
            self.assertEqual(result['status'],'PAUSED')
            self.assertEqual(result['requests'],{'ACCEPTED':2,'REJECTED':1,'NOT_REQUESTED':1})
            with self.assertRaisesRegex(ValueError,'blocks resume'):await self.b.start(db,task_id,resume=True)

    async def test_recovery_after_location_only_gets(self):
        task_id,run_id=await self.create()
        class Crash(BaseException): pass
        async def crash(step): raise Crash()
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.transport)) as client:
            with self.assertRaises(Crash): await self.b.run(task_id,run_id,client=client,after_receipt=crash)
            result=await self.b.run(task_id,run_id,client=client,poll_interval=0)
        self.assertEqual(result['requests'],{'RESULT':1})
        self.assertEqual(sum(x.startswith('POST') for x in self.calls),1)

    async def test_requested_without_location_becomes_unknown_and_blocks_resume(self):
        from sqlalchemy import select
        from backend.models import TraceStep
        task_id,run_id=await self.create()
        async with self.sessions() as db:
            step=(await db.execute(select(TraceStep).where(TraceStep.run_id==run_id))).scalar_one()
            step.output_data={'request_status':'REQUESTED','reserved_at':'offline-crash'}; await db.commit()
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.transport)) as client:
            result=await self.b.run(task_id,run_id,client=client)
        self.assertEqual(result['status'],'PAUSED'); self.assertEqual(self.calls,[])
        async with self.sessions() as db:
            with self.assertRaisesRegex(ValueError,'Uncertain'): await self.b.start(db,task_id,resume=True)

    async def test_same_task_cannot_create_second_run(self):
        task_id,run_id=await self.create()
        async with self.sessions() as db:
            with self.assertRaisesRegex(ValueError,'already exists'): await self.b.start(db,task_id)

    async def test_report_preserves_execution_commit_after_upgrade(self):
        with patch.dict(os.environ,{'AIAC_CODE_COMMIT':'execution-commit'}):
            task_id,run_id=await self.create()
        with patch.dict(os.environ,{'AIAC_CODE_COMMIT':'later-observer'}):
            async with self.sessions() as db:
                self.assertEqual((await self.b.report(db,run_id))['code_commit'],'execution-commit')

    async def test_429_stops_remaining_frozen_cases(self):
        task_id,run_id=await self.create([{'id':'a','expression':'rank(close)','decay':4},
                                        {'id':'b','expression':'rank(close)','decay':8}])
        calls=[]
        def limited(request):
            calls.append(request.method)
            return httpx.Response(429,json={'detail':'offline rate limit'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(limited)) as client:
            result=await self.b.run(task_id,run_id,client=client)
        self.assertEqual(result['status'],'PAUSED'); self.assertEqual(calls,['POST'])
        async with self.sessions() as db:
            report=await self.b.report(db,run_id)
            self.assertEqual(report['requests'],{'REJECTED':1,'NOT_REQUESTED':1})

    async def test_native_task_service_blocks_legacy_resume(self):
        from backend.models import MiningTask
        from backend.services.task_service import TaskService
        async with self.sessions() as db:
            task=MiningTask(task_name='legacy',region='USA',universe='TOP3000',status='PAUSED',config={})
            db.add(task);await db.commit()
            with self.assertRaisesRegex(ValueError,'legacy'):
                await TaskService(db).intervene_task(task.id,'RESUME')

    async def test_native_task_service_uses_frozen_start(self):
        from backend.models import MiningTask
        from backend.services.task_service import TaskService
        async with self.sessions() as db:
            task=MiningTask(task_name='native',region='USA',universe='TOP3000',status='PENDING',config={
                'execution_mode':'bounded','hypothesis':'offline','fields':['close'],'max_create_requests':1,
                'candidates':[{'id':'a','expression':'rank(close)'}]})
            db.add(task);await db.commit()
            with patch('backend.tasks.run_mining_task.delay',return_value=type('Job',(),{'id':'offline'})()):
                result=await TaskService(db).start_task(task.id)
            self.assertEqual((await self.b.report(db,result['run_id']))['requests'],{'NOT_REQUESTED':1})

    async def test_quality_pass_updates_task_progress(self):
        from backend.models import MiningTask
        task_id,run_id=await self.create()
        def transport(request):
            response=self.transport(request)
            if request.url.path.startswith('/alphas/'):
                data=response.json()
                data['is']={'sharpe':2,'fitness':1.5,'turnover':.1,'margin':.002,
                    'riskNeutralized':{'sharpe':2,'fitness':1.5},'checks':[
                        {'name':'LOW_2Y_SHARPE','result':'PASS','value':2},
                        {'name':'SELF_CORRELATION','result':'PASS','value':.2},
                        {'name':'PROD_CORRELATION','result':'PASS','value':.3}]}
                return httpx.Response(200,json=data)
            return response
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            result=await self.b.run(task_id,run_id,client=client,poll_interval=0)
        self.assertEqual(result['quality_PASS'],1)
        async with self.sessions() as db:
            self.assertEqual((await db.get(MiningTask,task_id)).progress_current,1)

    async def test_stop_during_last_alpha_get_is_preserved_with_result(self):
        from backend.models import MiningTask
        task_id,run_id=await self.create()
        async def transport(request):
            if request.url.path.startswith('/alphas/'):
                async with self.sessions() as db:
                    task=await db.get(MiningTask,task_id); task.status='STOPPED'; await db.commit()
            return self.transport(request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await self.b.run(task_id,run_id,client=client,poll_interval=0)
        async with self.sessions() as db:
            result=await self.b.report(db,run_id)
            self.assertEqual(result['status'],'STOPPED')
            self.assertEqual(result['requests'],{'RESULT':1})


if __name__=='__main__': unittest.main()
