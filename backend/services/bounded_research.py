"""Read-only compatibility for retired bounded research receipts. No execution."""
from datetime import datetime, timezone

def now():
    return datetime.now(timezone.utc).isoformat()

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


async def health():
    import asyncio
    from backend.celery_app import celery_app
    replies = await asyncio.to_thread(lambda: celery_app.control.inspect(timeout=2).ping() or {})
    return {'execution_mode': 'native', 'worker_online': bool(replies), 'workers': list(replies)}


async def notifications(db):
    from sqlalchemy import select
    from backend.models import TraceStep
    rows = (await db.execute(select(TraceStep).where(TraceStep.step_type=='REVIEW')
                            .order_by(TraceStep.id.desc()).limit(50))).scalars().all()
    return [{'id': s.id, 'task_id': s.task_id, 'run_id': s.run_id, **s.output_data} for s in rows]
