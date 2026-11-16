"""Unlimited total research through bounded native runs; no resident agent/scheduler."""
import asyncio
import json
from sqlalchemy import select, text
from backend.database import AsyncSessionLocal
from backend.models import MiningTask, ExperimentRun, TraceStep, Alpha
from backend.services import bounded_research as bounded

PLAN_LOCK=1120261118


async def verify_fields(fields,region,universe):
    from backend.adapters.brain_adapter import BrainAdapter
    try:
        async with BrainAdapter() as adapter:
            for field in fields:
                response=await adapter.client.get(adapter.BASE_URL+'/data-fields',params={
                    'search':field,'region':region,'universe':universe,'delay':1,'instrumentType':'EQUITY','limit':50},timeout=30)
                if response.status_code!=200 or not any(x.get('id')==field and x.get('type')=='MATRIX'
                    for x in response.json().get('results',[])):
                    raise ValueError('Field catalog unavailable or incompatible')
    finally:
        await BrainAdapter.close()


async def propose(context):
    from backend.agents.services.llm_service import LLMService
    service=LLMService()
    try:
        response=await asyncio.wait_for(service.call(
            system_prompt=('你是连续 Alpha 研究的候选规划器。仅依据输入数据和真实回测结果规划，不执行工具。'
                '输出 JSON: hypothesis, family(稳定英文机制名), fields(1到2个已验证字段), candidates([{id,expression,decay}])。'
                '严格生成 batch_size 个不同候选，FASTEXPR；每式最多5个算子，不用trade_when，不事后翻转失败信号的符号。'
                '使用 catalog 内字段以及分组变量industry/sector/subindustry；不凭空编造字段。'
                '同批保持一致的经济机制和方向，预先说明对照设计；每个候选须有经济理由，不做无依据的全窗口穷举。'
                'STOP的机制和失败三轮的机制必须换机制；禁止旧 receivables/revenue 家族与 returns-volume相关性反转家族。'
                '避免 excluded_expressions，不把更多回测或HTTP成功当作质量。无总批次数上限，但不得绕过限流、重复提交或提交Alpha。'),
            user_prompt=json.dumps(context,ensure_ascii=False),temperature=.5,json_mode=True,max_tokens=6000),timeout=90)
        if not response.success or not isinstance(response.parsed,dict):raise ValueError('Planner returned no valid JSON')
        return response.parsed
    finally: await service.client.close()


async def advance(task_id, previous_run_id):
    async with AsyncSessionLocal() as db:
        if not await db.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'),{'key':PLAN_LOCK}):return
        task=await db.get(MiningTask,task_id)
        if not task or task.status!='RUNNING' or task.config.get('continuous') is not True:return
        runs=(await db.execute(select(ExperimentRun).where(ExperimentRun.task_id==task_id).order_by(ExperimentRun.id.desc()))).scalars().all()
        if not runs or runs[0].id!=previous_run_id or runs[0].status!='COMPLETED':return
        previous=runs[0]
        review=(await db.execute(select(TraceStep.output_data).where(TraceStep.run_id==previous_run_id,TraceStep.step_type=='REVIEW'))).scalar_one_or_none()
        if not review:
            from backend.services.completion_review import complete
            await complete(previous_run_id)
            review=(await db.execute(select(TraceStep.output_data).where(TraceStep.run_id==previous_run_id,TraceStep.step_type=='REVIEW'))).scalar_one_or_none()
        try:
            if not review or review.get('analysis_status')!='COMPLETED':raise ValueError('Review unavailable; continuation paused')
            catalog=task.config.get('verified_fields',[])
            if not catalog:raise ValueError('No verified field catalog')
            summaries=[]; failed_families={}
            for run in runs:
                manifest=run.config_snapshot.get('bounded_manifest',{})
                results=(await db.execute(select(Alpha).where(Alpha.run_id==run.id))).scalars().all()
                family=manifest.get('family','unspecified')
                passed=sum(a.quality_status=='PASS' for a in results)
                if results and not passed:failed_families[family]=failed_families.get(family,0)+1
                if len(summaries)<12:
                    summaries.append({'run_id':run.id,'family':family,'hypothesis':manifest.get('hypothesis'),
                        'alphas':[{'expression':a.expression,'quality':a.quality_status,'sharpe':a.is_sharpe,
                                   'fitness':a.is_fitness,'failures':(a.metrics or {}).get('failures',[])} for a in results]})
            context={'catalog':catalog,'region':task.region,'universe':task.universe,'batch_size':task.config['max_create_requests'],
                     'recent_batches':summaries,'review':review['analysis'],'failed_family_counts':failed_families,
                     'excluded_expressions':(await db.execute(select(Alpha.expression).order_by(Alpha.id.desc()).limit(250))).scalars().all()}
            plan=await propose(context)
            if not isinstance(plan.get('family'),str) or not plan['family'].strip():raise ValueError('Missing stable mechanism name')
            if len(plan.get('candidates',[]))!=task.config['max_create_requests']:raise ValueError('Wrong batch size')
            if not set(plan.get('fields',[])).issubset(catalog):raise ValueError('Unverified fields')
            if failed_families.get(plan['family'],0)>=3:raise ValueError('Failed family exhausted')
            old_family=previous.config_snapshot['bounded_manifest'].get('family')
            if review['analysis']['decision']=='STOP' and plan['family']==old_family:raise ValueError('Stopped mechanism must change')
            await verify_fields(plan['fields'],task.region,task.universe)
            # Explicit owner controls beat model output. No total request/iteration limit.
            config={**task.config,**{k:plan[k] for k in ('hypothesis','family','fields','candidates')}}
            await db.refresh(task)
            if task.status!='RUNNING':return
            return await bounded.start(db,task_id,next_config=config,previous_run_id=previous_run_id)
        except Exception as exc:
            await db.rollback()
            await db.refresh(task)
            if task.status=='RUNNING':
                task.status='PAUSED'
                task.config={**task.config,'continuation_error':type(exc).__name__+': '+(str(exc)[:250] if isinstance(exc,ValueError) else 'planning unavailable')}
                db.add(TraceStep(task_id=task_id,run_id=previous_run_id,step_type='PLAN',step_order=100,
                    status='FAILED',output_data={'reason':task.config['continuation_error'],'no_new_simulation':True}))
                await db.commit()


async def recover():
    from backend.tasks.mining_tasks import continue_research
    async with AsyncSessionLocal() as db:
        tasks=(await db.execute(select(MiningTask).where(MiningTask.status=='RUNNING'))).scalars()
        for task in tasks:
            if task.config.get('continuous') is not True:continue
            run=(await db.execute(select(ExperimentRun).where(ExperimentRun.task_id==task.id).order_by(ExperimentRun.id.desc()).limit(1))).scalar_one_or_none()
            if run and run.status=='COMPLETED':continue_research.delay(task.id,run.id)
