"""
Celery Application for Background Tasks
Handles mining tasks, feedback loops, and scheduled jobs
"""

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_ready, worker_shutdown
import asyncio
import hashlib
import json
import redis
from uuid import uuid4
from loguru import logger

from backend.config import settings

# Create Celery app
celery_app = Celery(
    "aiac",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["backend.tasks"]
)

# Celery configuration
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=settings.MINING_TASK_TIME_LIMIT_SECONDS,
    worker_prefetch_multiplier=1,  # Fair scheduling
)


def _worker_credentials_status_key(hostname: str) -> str:
    return f"aiac:credential-runtime:{hostname}"


def _publish_worker_credentials_status(hostname: str, remove: bool = False) -> None:
    """Publish only readiness and a one-way secret fingerprint to shared Redis."""
    client = None
    try:
        client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        key = _worker_credentials_status_key(hostname)
        if remove:
            client.delete(key)
            return
        secret = settings.CREDENTIALS_SECRET or ""
        fingerprint = hashlib.sha256(secret.encode()).hexdigest() if secret else ""
        client.set(key, json.dumps({"configured": bool(secret), "fingerprint": fingerprint}))
    except Exception as exc:
        logger.warning(f"Could not publish credential worker status ({type(exc).__name__})")
    finally:
        if client is not None:
            client.close()


async def _recover_unfinished_runs() -> int:
    """Requeue active DB-backed runs after a sole worker restart."""
    from sqlalchemy import select
    from backend.database import AsyncSessionLocal
    from backend.models import ExperimentRun, MiningTask

    recovered = 0
    async with AsyncSessionLocal() as db:
        runs = (await db.execute(
            select(ExperimentRun)
            .join(MiningTask, ExperimentRun.task_id == MiningTask.id)
            .where(
                ExperimentRun.status.in_(("RUNNING", "WAITING_LLM")),
                MiningTask.status == ExperimentRun.status,
            )
        )).scalars().all()
        for run in runs:
            old_dispatch_id = run.celery_task_id
            dispatch_id = str(uuid4())
            run.celery_task_id = dispatch_id
            await db.commit()
            try:
                celery_app.send_task(
                    "backend.tasks.run_mining_task",
                    args=[run.task_id, run.id],
                    task_id=dispatch_id,
                )
                recovered += 1
            except Exception:
                run.celery_task_id = old_dispatch_id
                await db.commit()
                logger.error(f"Could not restore active run | run={run.id}")
    return recovered


@worker_ready.connect
def _on_worker_ready(sender=None, **kwargs):
    hostname = getattr(sender, "hostname", None)
    if hostname:
        _publish_worker_credentials_status(hostname)
    try:
        recovered = asyncio.run(_recover_unfinished_runs())
        if recovered:
            logger.info(f"Restored {recovered} active run(s) on worker startup")
    except Exception as exc:
        logger.error(f"Could not scan active runs on worker startup ({type(exc).__name__})")


@worker_shutdown.connect
def _on_worker_shutdown(sender=None, **kwargs):
    hostname = getattr(sender, "hostname", None)
    if hostname:
        _publish_worker_credentials_status(hostname, remove=True)

# Scheduled tasks (Celery Beat)
celery_app.conf.beat_schedule = {
    # Daily feedback analysis at 23:00
    "daily-feedback-analysis": {
        "task": "backend.tasks.run_daily_feedback",
        "schedule": crontab(hour=23, minute=0),
    },
    # Update operator stats every 6 hours
    "update-operator-stats": {
        "task": "backend.tasks.update_operator_stats",
        "schedule": crontab(hour="*/6", minute=0),
    },
    # Sync datasets from BRAIN daily at 06:00
    "sync-datasets": {
        "task": "backend.tasks.sync_datasets",
        "schedule": crontab(hour=6, minute=0),
    },
    # Import curated academic/forum-derived knowledge daily at 05:30
    "sync-external-knowledge": {
        "task": "backend.tasks.sync_external_knowledge",
        "schedule": crontab(hour=5, minute=30),
    },
}
