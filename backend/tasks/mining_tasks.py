"""
Mining Tasks - Background tasks for alpha mining

Contains the main mining task execution logic.
"""

from datetime import datetime, timedelta, timezone
from math import ceil
from sqlalchemy import select, update, desc
from loguru import logger
from celery.signals import task_failure

from backend.config import settings
from backend.celery_app import celery_app
from backend.database import AsyncSessionLocal
from backend.agents import MiningAgent
from backend.agents.services.llm_service import LLMBlockedError
from backend.services.credentials_service import CredentialsService
from backend.adapters.mcp_brain_adapter import MCPBrainAdapter
from backend.models import MiningTask, DatasetMetadata, Operator, DataField, ExperimentRun
from backend.tasks import run_async


def _slice_goal_and_prior_success(
    checkpoint: dict, dataset_id: str, remaining_goal: int,
) -> tuple[int, int]:
    """Keep a run's cumulative success target stable across worker yields."""
    prior_success = (
        int(checkpoint.get("total_success", 0))
        if checkpoint.get("dataset_id") == dataset_id else 0
    )
    return prior_success, prior_success + remaining_goal


def _new_passes_in_slice(result: dict, prior_success: int) -> int:
    current_success = int(result.get("total_success", 0))
    if current_success < prior_success:
        raise ValueError("Run success counter moved backwards across a worker yield")
    return current_success - prior_success


def _next_wait_minutes(kind: str, quota_attempts: int) -> int | None:
    if kind == "DAILY_QUOTA":
        return (15, 30, 60)[min(max(quota_attempts, 0), 2)]
    if kind in {"NO_NEW_CANDIDATES", "DEDUP_UNAVAILABLE", "WQ_SIMULATION_ERROR"}:
        return (5, 15, 30, 60)[min(max(quota_attempts, 0), 3)]
    if kind == "INVALID_CREDENTIALS":
        return None
    return 5


def _next_wait_seconds(kind: str, attempts: int) -> int | None:
    if kind in {"MALFORMED_HYPOTHESIS", "MALFORMED_RESPONSE"}:
        delays = (30, 120, 300, 900, 1800, 3600)
        return delays[min(max(attempts, 0), len(delays) - 1)]
    if kind == "CONNECTION_ERROR":
        delays = (30, 120, 300, 900)
        return delays[min(max(attempts, 0), len(delays) - 1)]
    minutes = _next_wait_minutes(kind, attempts)
    return None if minutes is None else minutes * 60


def _retry_poll_seconds(next_retry_at, now=None) -> int:
    """Poll at the deadline for short waits, and at most every five minutes otherwise."""
    if next_retry_at is None:
        return 300
    now = now or datetime.now(timezone.utc)
    if next_retry_at.tzinfo is None:
        next_retry_at = next_retry_at.replace(tzinfo=timezone.utc)
    return max(1, min(300, ceil((next_retry_at - now).total_seconds())))


async def _schedule_llm_wait(
    db, task: MiningTask, run: ExperimentRun, kind: str, revision: int,
    safe_detail: str | None = None,
) -> dict:
    """Release the worker and retry the same checkpoint without fake candidates."""
    checkpoint = run.checkpoint or {}
    quota_attempts = int(checkpoint.get("quota_attempts", 0))
    malformed_attempts = int(checkpoint.get("malformed_hypothesis_attempts", 0))
    malformed_response_attempts = int(checkpoint.get("malformed_response_attempts", 0))
    recovery_wait_attempts = int(checkpoint.get("recovery_wait_attempts", 0))
    connection_attempts = int(checkpoint.get("connection_attempts", 0))
    if kind != "CONNECTION_ERROR":
        connection_attempts = 0
    if kind == "MALFORMED_HYPOTHESIS":
        if checkpoint.get("malformed_hypothesis_iteration") != checkpoint.get("iteration"):
            malformed_attempts = 0
        delay_seconds = _next_wait_seconds(kind, malformed_attempts)
        malformed_attempts += 1
    elif kind == "MALFORMED_RESPONSE":
        if checkpoint.get("malformed_response_iteration") != checkpoint.get("iteration"):
            malformed_response_attempts = 0
        delay_seconds = _next_wait_seconds(kind, malformed_response_attempts)
        malformed_response_attempts += 1
    elif kind in {"NO_NEW_CANDIDATES", "DEDUP_UNAVAILABLE", "WQ_SIMULATION_ERROR"}:
        delay_seconds = _next_wait_seconds(kind, recovery_wait_attempts)
        recovery_wait_attempts += 1
    elif kind == "CONNECTION_ERROR":
        delay_seconds = _next_wait_seconds(kind, connection_attempts)
        connection_attempts += 1
    else:
        delay_seconds = _next_wait_seconds(kind, quota_attempts)
    if kind == "DAILY_QUOTA":
        quota_attempts += 1
    run.checkpoint = {
        **checkpoint,
        "quota_attempts": quota_attempts,
        "malformed_hypothesis_attempts": malformed_attempts,
        "malformed_hypothesis_iteration": checkpoint.get("iteration"),
        "malformed_response_attempts": malformed_response_attempts,
        "malformed_response_iteration": checkpoint.get("iteration"),
        "recovery_wait_attempts": recovery_wait_attempts,
        "connection_attempts": connection_attempts,
        "connection_error_type": safe_detail if kind == "CONNECTION_ERROR" and safe_detail in {"TIMEOUT", "CONNECT"} else None,
        "credential_revision": revision,
    }
    run.status = "WAITING_LLM"
    run.block_reason = kind
    run.next_retry_at = (
        datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        if delay_seconds is not None else None
    )
    task.status = "WAITING_LLM"
    await db.commit()
    logger.warning(
        f"LLM wait | task={task.id} run={run.id} reason={kind} "
        f"detail={run.checkpoint['connection_error_type'] or 'NONE'} "
        f"next_retry_at={run.next_retry_at}"
    )
    return {"_retry": True, "run_id": run.id,
            "retry_countdown": _retry_poll_seconds(run.next_retry_at)}


def _wait_is_due(next_retry_at, saved_revision: int, current_revision: int, now=None) -> bool:
    if current_revision != saved_revision:
        return True
    if next_retry_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    if next_retry_at.tzinfo is None:
        next_retry_at = next_retry_at.replace(tzinfo=timezone.utc)
    return now >= next_retry_at


@celery_app.task(bind=True, name="backend.tasks.run_mining_task", max_retries=None)
def run_mining_task(self, task_id: int, run_id: int | None = None):
    """
    Run a complete mining task.
    Called when a task is started via API.
    
    Args:
        task_id: The mining task ID
        run_id: Optional experiment run ID
    """
    logger.info(f"Starting mining task {task_id} (run_id={run_id})")
    
    async def _run():
        async with AsyncSessionLocal() as db:
            run: ExperimentRun | None = None

            # Get task
            query = select(MiningTask).where(MiningTask.id == task_id)
            result = await db.execute(query)
            task = result.scalar_one_or_none()
            
            if not task:
                logger.error(f"Task {task_id} not found")
                return {"error": "Task not found"}

            existing_run = await db.get(ExperimentRun, run_id) if run_id else None
            if existing_run and existing_run.celery_task_id and existing_run.celery_task_id != self.request.id:
                logger.info(f"Ignoring stale mining dispatch | task={task_id} run={run_id}")
                return {"skipped": "stale dispatch"}

            if task.status not in {"RUNNING", "WAITING_LLM"}:
                logger.info(f"Ignoring queued mining dispatch | task={task_id} status={task.status}")
                return {"skipped": task.status}

            if task.status == "WAITING_LLM":
                waiting_run = await db.get(ExperimentRun, run_id) if run_id else None
                if not waiting_run or waiting_run.status != "WAITING_LLM":
                    return {"skipped": "stale wait"}
                revision = await CredentialsService(db).get_revision()
                checkpoint = waiting_run.checkpoint or {}
                credentials_changed = revision != checkpoint.get("credential_revision")
                if not _wait_is_due(waiting_run.next_retry_at, checkpoint.get("credential_revision"), revision):
                    return {"_retry": True, "run_id": run_id,
                            "retry_countdown": _retry_poll_seconds(waiting_run.next_retry_at)}
                if credentials_changed:
                    waiting_run.checkpoint = {
                        **checkpoint, "quota_attempts": 0,
                        "malformed_hypothesis_attempts": 0,
                        "malformed_response_attempts": 0,
                        "recovery_wait_attempts": 0,
                        "connection_attempts": 0,
                        "connection_error_type": None,
                        "credential_revision": revision,
                    }
                    logger.info(f"Credential revision changed; waking task={task_id} run={run_id}")
            
            # Update status to RUNNING
            claimed = await db.execute(
                update(MiningTask)
                .where(MiningTask.id == task_id)
                .where(MiningTask.status == task.status)
                .values(status="RUNNING")
            )
            if claimed.rowcount != 1:
                await db.rollback()
                return {"skipped": "task status changed"}
            await db.commit()
            await db.refresh(task)
            if task.status != "RUNNING":
                return {"skipped": task.status}

            # Create or attach ExperimentRun
            run = await _get_or_create_run(db, task, run_id, self.request.id)
            active_run_id = run.id

            try:
                try:
                    from backend.alpha_semantic_validator import load_operators_from_db
                    operators_loaded = await load_operators_from_db(db)
                    logger.info(f"Operator registry loaded in worker: {len(operators_loaded)} operators")
                except Exception as e:
                    logger.warning(f"Failed to load operator registry in worker: {e}")

                async with MCPBrainAdapter() as brain:
                    if (task.config or {}).get("research_protocol") == "paper_news17_ablation_v1":
                        from backend.services.paper_experiment import run_paper_dispatch

                        fields = await _get_dataset_fields(db, "news17", task.region, task.universe)
                        paper_outcome = await run_paper_dispatch(db, brain, task, run, fields)
                        if paper_outcome.get("done"):
                            return {"success": True, "run_id": run.id,
                                    "research_reason": paper_outcome.get("reason")}
                        await db.refresh(task)
                        if task.status in {"PAUSED", "STOPPED"}:
                            return {"skipped": task.status}
                        return {"_retry": True, "run_id": run.id, "retry_countdown": 1}

                    if (task.config or {}).get("audit_candidates"):
                        from backend.services.research_audit import ResearchBudgetBrain, ResearchLedger

                        continuous_mode = (task.config or {}).get("continuous_mode") is True
                        brain = ResearchBudgetBrain(brain, ResearchLedger(
                            task.id, run.id,
                            None if continuous_mode else int((task.config or {}).get("research_request_budget", 1_000_000)),
                            str((task.config or {}).get("research_family_id") or "unclassified"),
                            dedup_task_ids=(task.config or {}).get("dedup_task_ids", []) if continuous_mode else [],
                        ))
                    continuous_mode = (task.config or {}).get("continuous_mode") is True
                    mining_agent = MiningAgent(db, brain)
                    
                    # Get datasets to mine
                    datasets = await _get_datasets_to_mine(db, task)
                    resume_dataset = (run.checkpoint or {}).get("dataset_id") if run else None
                    if resume_dataset in datasets:
                        datasets = datasets[datasets.index(resume_dataset):]
                    
                    if not datasets:
                        logger.warning(f"No datasets found for mining in {task.region}/{task.universe}")
                        if continuous_mode:
                            task.status = run.status = "PAUSED"
                            run.block_reason = "CONFIG_DATASET_UNAVAILABLE"
                            await db.commit()
                            return {"skipped": "CONFIG_DATASET_UNAVAILABLE", "run_id": run.id}
                        await _mark_mining_task_failed(
                            db,
                            task_id=task_id,
                            run_id=run.id if run is not None else None,
                            celery_task_id=self.request.id,
                            error_message="No datasets found",
                        )
                        return {"warning": "No datasets found"}

                    # Get operators
                    operators = await _get_operators(db)
                    
                    # Mine each dataset
                    total_alphas = 0
                    target_reached = not continuous_mode and task.progress_current >= task.daily_goal
                    incomplete_reasons = []
                    for dataset_id in datasets:
                        # Check if task should continue
                        await db.refresh(task)
                        if task.status in ["STOPPED", "PAUSED"]:
                            logger.info(f"Task {task_id} {task.status}, stopping")
                            break
                        
                        if not continuous_mode and task.progress_current >= task.daily_goal:
                            logger.info(f"Task {task_id} reached goal")
                            break
                        
                        # Get fields
                        fields = await _get_dataset_fields(db, dataset_id, task.region, task.universe)
                        
                        if not fields:
                            logger.warning(f"No fields found for dataset {dataset_id}, skipping")
                            if continuous_mode:
                                task.status = run.status = "PAUSED"
                                run.block_reason = "CONFIG_FIELD_UNAVAILABLE"
                                await db.commit()
                                return {"skipped": "CONFIG_FIELD_UNAVAILABLE", "run_id": run.id}
                            continue
                        
                        # Calculate remaining alphas needed
                        remaining_goal = max(1, task.daily_goal - task.progress_current) if continuous_mode else task.daily_goal - task.progress_current
                        if remaining_goal <= 0:
                            logger.info(f"Task {task_id} already reached goal, stopping")
                            break
                        
                        # Run one complete round, then release the solo worker.
                        prior_success, absolute_goal = _slice_goal_and_prior_success(
                            run.checkpoint or {}, dataset_id, remaining_goal,
                        )
                        try:
                            result = await mining_agent.run_evolution_loop(
                                task=task,
                                dataset_id=dataset_id,
                                fields=fields,
                                operators=operators,
                                max_iterations=task.max_iterations or 10,
                                target_alphas=absolute_goal,
                                num_alphas_per_round=4,
                                run_id=run.id,
                                max_rounds_per_dispatch=1,
                                continuous_mode=continuous_mode,
                            )
                            
                            # The agent returns cumulative dataset success;
                            # only the delta belongs to this dispatch.
                            task.progress_current += _new_passes_in_slice(result, prior_success)
                            await db.commit()
                            
                            total_alphas += len(result.get("all_alphas", []))
                            target_reached = not continuous_mode and task.progress_current >= task.daily_goal

                            if result.get("yielded_for_dispatch"):
                                await db.refresh(task)
                                if task.status in {"PAUSED", "STOPPED"}:
                                    return {"skipped": task.status}
                                logger.info(
                                    f"Continuing task={task_id} run={run.id} "
                                    f"after completed round={result.get('iterations_completed')}"
                                )
                                return {"_retry": True, "run_id": run.id, "retry_countdown": 1}

                            if not continuous_mode and not result.get("target_reached"):
                                incomplete_reasons.append(
                                    (
                                        f"{dataset_id}: target not reached "
                                        f"({result.get('total_success', 0)}/{remaining_goal}) "
                                        f"after {result.get('iterations_completed', 0)}/"
                                        f"{task.max_iterations or 10} iterations"
                                    )
                                )
                                if result.get("dataset_stop_reason"):
                                    incomplete_reasons[-1] += (
                                        f"; stop_reason={result.get('dataset_stop_reason')}"
                                    )
                            
                            logger.info(
                                f"Evolution loop for {dataset_id} complete | "
                                f"iterations={result.get('iterations_completed')} "
                                f"success={result.get('total_success')}"
                            )
                            
                            if target_reached:
                                logger.info(f"Task {task_id} reached goal via evolution loop")
                                break
                                
                        except LLMBlockedError as exc:
                            await db.rollback()
                            await db.refresh(task)
                            if task.status in {"PAUSED", "STOPPED"}:
                                return {"skipped": task.status}
                            revision = await CredentialsService(db).get_revision()
                            await db.refresh(run)
                            return await _schedule_llm_wait(
                                db, task, run, exc.kind, revision, exc.safe_detail,
                            )
                        except Exception as e:
                            logger.error(f"Evolution loop failed for {dataset_id}: {e}")
                            # Rollback any failed transaction before continuing
                            await db.rollback()
                            continue
                
                # Preserve user intervention state. A STOP/PAUSE can arrive
                # while a BRAIN multi-simulation is still polling; once that
                # call returns, this task must not overwrite it as COMPLETED.
                await db.refresh(task)
                if continuous_mode and task.status == "RUNNING":
                    # A transient dataset or generation failure must not complete an
                    # open-ended run. Release the worker and retry the same run.
                    return {"_retry": True, "run_id": active_run_id, "retry_countdown": 60}
                final_status, error_message = _resolve_final_status(
                    task_status=task.status,
                    target_reached=target_reached,
                    incomplete_reasons=incomplete_reasons,
                    progress_current=task.progress_current,
                    daily_goal=task.daily_goal,
                )
                if task.status in ["STOPPED", "PAUSED"]:
                    logger.info(f"Task {task_id} ended with intervention status {final_status}")
                else:
                    if final_status == "COMPLETED" and error_message:
                        logger.warning(
                            f"Task {task_id} completed all configured mining work "
                            f"without reaching target: {error_message}"
                        )

                    await db.execute(
                        update(MiningTask)
                        .where(MiningTask.id == task_id)
                        .values(status=final_status, updated_at=datetime.utcnow())
                    )

                if run is not None:
                    run.status = final_status
                    run.finished_at = datetime.utcnow()
                    if error_message:
                        run.error_message = error_message
                await db.commit()
                
                logger.info(f"Task {task_id} finished as {final_status}: {total_alphas} alphas mined")
                return {"success": True, "alphas_mined": total_alphas}
                
            except Exception as e:
                logger.error(f"Task {task_id} failed: {e}")
                # Rollback any failed transaction before updating status
                await db.rollback()
                
                try:
                    await _mark_mining_task_failed(
                        db,
                        task_id=task_id,
                        run_id=active_run_id,
                        celery_task_id=self.request.id,
                        error_message=str(e)[:500],
                    )
                except Exception as db_err:
                    logger.error(f"Failed to update task status: {db_err}")
                    await db.rollback()
                raise
    
    outcome = run_async(_run())
    if isinstance(outcome, dict) and outcome.get("_retry"):
        # Celery releases the worker between checks. The same run id is reused.
        raise self.retry(countdown=outcome.get("retry_countdown", 300),
                         args=(task_id, outcome["run_id"]), max_retries=None)
    return outcome


def _resolve_final_status(
    *,
    task_status: str | None = None,
    target_reached: bool,
    incomplete_reasons: list[str],
    progress_current: int,
    daily_goal: int,
) -> tuple[str, str | None]:
    """Resolve task status after the mining workflow returns normally."""
    if task_status in ["STOPPED", "PAUSED"]:
        return task_status, None
    if target_reached:
        return "COMPLETED", None
    if incomplete_reasons:
        return "COMPLETED", ("; ".join(incomplete_reasons))[:500]
    return "FAILED", f"Target not reached: {progress_current}/{daily_goal}"[:500]


@task_failure.connect
def _sync_mining_task_failure_state(sender=None, task_id=None, exception=None, args=None, kwargs=None, **_):
    """Persist FAILED state for Celery-level failures such as hard time limits."""
    if getattr(sender, "name", None) != "backend.tasks.run_mining_task":
        return

    args = args or ()
    kwargs = kwargs or {}

    mining_task_id = kwargs.get("task_id")
    run_id = kwargs.get("run_id")
    if mining_task_id is None and len(args) >= 1:
        mining_task_id = args[0]
    if run_id is None and len(args) >= 2:
        run_id = args[1]

    if mining_task_id is None:
        logger.error(f"Mining task failure signal missing task id | celery_task_id={task_id}")
        return

    error_message = f"{type(exception).__name__}: {exception}"[:500]

    async def _mark_failed_from_signal():
        async with AsyncSessionLocal() as db:
            await _mark_mining_task_failed(
                db,
                task_id=int(mining_task_id),
                run_id=int(run_id) if run_id is not None else None,
                celery_task_id=task_id,
                error_message=error_message,
            )

    try:
        run_async(_mark_failed_from_signal())
    except Exception as err:
        logger.error(
            "Failed to sync mining task failure state from Celery signal | "
            f"task_id={mining_task_id} run_id={run_id} celery_task_id={task_id}: {err}"
        )


async def _mark_mining_task_failed(
    db,
    task_id: int,
    run_id: int | None,
    celery_task_id: str | None,
    error_message: str,
):
    """Mark a mining task/run as failed without overwriting user intervention states."""
    now = datetime.utcnow()
    error_message = (error_message or "Mining task failed")[:500]

    await db.execute(
        update(MiningTask)
        .where(MiningTask.id == task_id)
        .where(MiningTask.status == "RUNNING")
        .values(status="FAILED", updated_at=now)
    )

    run_query = update(ExperimentRun).where(ExperimentRun.status.in_(["RUNNING", "WAITING_LLM"]))
    if run_id is not None:
        run_query = run_query.where(ExperimentRun.id == run_id)
    elif celery_task_id:
        run_query = run_query.where(ExperimentRun.celery_task_id == celery_task_id)
    else:
        run_query = run_query.where(ExperimentRun.task_id == task_id)

    await db.execute(
        run_query.values(
            status="FAILED",
            finished_at=now,
            error_message=error_message,
        )
    )
    await db.commit()


async def _get_or_create_run(db, task, run_id, celery_task_id):
    """Get or create an experiment run."""
    if run_id is not None:
        run_query = select(ExperimentRun).where(ExperimentRun.id == run_id)
        run_res = await db.execute(run_query)
        run = run_res.scalar_one_or_none()

        if run and run.task_id != task.id:
            raise ValueError(f"ExperimentRun {run_id} does not belong to task {task.id}")

        if run is None:
            run = ExperimentRun(
                id=run_id,
                task_id=task.id,
                status="RUNNING",
                trigger_source="API",
                celery_task_id=celery_task_id,
                config_snapshot=_create_config_snapshot(task),
                strategy_snapshot={},
            )
            db.add(run)
            await db.commit()
            await db.refresh(run)
        else:
            run.status = "RUNNING"
            run.trigger_source = "API"
            run.celery_task_id = celery_task_id
            run.error_message = None
            run.block_reason = None
            run.next_retry_at = None
            run.finished_at = None
            await db.commit()
    else:
        run = ExperimentRun(
            task_id=task.id,
            status="RUNNING",
            trigger_source="API",
            celery_task_id=celery_task_id,
            config_snapshot=_create_config_snapshot(task),
            strategy_snapshot={},
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
    
    return run


def _create_config_snapshot(task):
    """Create a config snapshot for experiment run."""
    return {
        "task": {
            "region": task.region,
            "universe": task.universe,
            "dataset_strategy": task.dataset_strategy,
            "target_datasets": task.target_datasets,
            "daily_goal": task.daily_goal,
            "config": task.config,
        },
    }


async def _get_datasets_to_mine(db, task):
    """Get list of dataset IDs to mine."""
    if task.dataset_strategy == "SPECIFIC" and task.target_datasets:
        return task.target_datasets

    config = task.config or {}
    delay = config.get("delay")
    include_categories = _config_list(config, "include_categories", "target_categories")
    exclude_categories = _config_list(
        config,
        "exclude_categories",
        "avoid_categories",
        "lit_categories",
        "already_lit_categories",
    )
    exclude_categories.extend(_csv_list(settings.MINING_EXCLUDE_CATEGORIES))
    exclude_categories = sorted({c.lower() for c in exclude_categories if c})

    # Auto-explore: get top datasets by weight, honoring task-level pyramid/category filters.
    ds_query = (
        select(DatasetMetadata)
        .where(
            DatasetMetadata.region == task.region,
            DatasetMetadata.universe == task.universe,
            DatasetMetadata.is_active == True,
        )
        .order_by(
            DatasetMetadata.mining_weight.desc(),
            desc(DatasetMetadata.pyramid_multiplier),
            desc(DatasetMetadata.value_score),
            desc(DatasetMetadata.coverage),
            DatasetMetadata.alpha_count.asc(),
        )
        .limit(10)
    )
    if delay is not None:
        ds_query = ds_query.where(DatasetMetadata.delay == int(delay))
    if include_categories:
        ds_query = ds_query.where(DatasetMetadata.category.in_(include_categories))
    if exclude_categories:
        ds_query = ds_query.where(~DatasetMetadata.category.in_(exclude_categories))

    ds_result = await db.execute(ds_query)
    datasets_objs = ds_result.scalars().all()
    datasets = [d.dataset_id for d in datasets_objs]
    logger.info(
        "[MiningTask] AUTO dataset selection | task={} selected={} include_categories={} "
        "exclude_categories={} delay={}",
        task.id,
        datasets,
        include_categories,
        exclude_categories,
        delay,
    )
    return datasets


def _config_list(config, *keys):
    """Read a list-like config value from the first present key."""
    for key in keys:
        if key in config:
            return _csv_list(config.get(key))
    return []


def _csv_list(value):
    """Normalize comma-separated strings and JSON arrays to a string list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


async def _get_operators(db):
    """Get operators for mining."""
    op_query = select(Operator).where(Operator.is_active == True)
    op_result = await db.execute(op_query)
    
    operators = []
    for op in op_result.scalars().all():
        operators.append({
            "name": op.name,
            "category": op.category,
            "description": op.description,
            "definition": op.definition,
            "scope": op.scope,
        })
    
    if not operators:
        # Fallback if DB is empty
        logger.warning("No operators found in DB, using basic set")
        operators = [
            {"name": "ts_rank", "category": "Time Series", "description": "Rank over time", "definition": "ts_rank(x, d)", "scope": ["REGULAR"]},
            {"name": "ts_mean", "category": "Time Series", "description": "Mean over time", "definition": "ts_mean(x, d)", "scope": ["REGULAR"]},
            {"name": "ts_std_dev", "category": "Time Series", "description": "Std Dev over time", "definition": "ts_std_dev(x, d)", "scope": ["REGULAR"]},
            {"name": "ts_corr", "category": "Time Series", "description": "Correlation", "definition": "ts_corr(x, y, d)", "scope": ["REGULAR"]},
            {"name": "ts_product", "category": "Time Series", "description": "Product over time", "definition": "ts_product(x, d)", "scope": ["REGULAR"]},
            {"name": "ts_sum", "category": "Time Series", "description": "Sum over time", "definition": "ts_sum(x, d)", "scope": ["REGULAR"]}
        ]
    
    return operators


async def _get_dataset_fields(db, dataset_id, region, universe):
    """Get fields for a dataset."""
    ds_meta_stmt = select(DatasetMetadata).where(
        DatasetMetadata.dataset_id == dataset_id,
        DatasetMetadata.region == region,
        DatasetMetadata.universe == universe
    )
    ds_meta_res = await db.execute(ds_meta_stmt)
    ds_meta = ds_meta_res.scalar_one_or_none()
    
    if not ds_meta:
        return []
    
    fields_stmt = select(DataField).where(DataField.dataset_id == ds_meta.id)
    fields_res = await db.execute(fields_stmt)
    fields_objs = fields_res.scalars().all()
    
    return [
        {
            "id": f.field_id,
            "name": f.field_name,
            "description": f.description,
            "type": f.field_type,
            "coverage": f.coverage,
            "date_coverage": f.date_coverage,
            "alpha_count": f.alpha_count,
            "user_count": f.user_count,
            "pyramid_multiplier": f.pyramid_multiplier,
            "category": f.category,
            "subcategory": f.subcategory,
        }
        for f in fields_objs
    ]
