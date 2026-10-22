"""
Runs Router - Experiment run management

Uses RunService for all business logic.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime

from backend.database import get_db
from backend.services.run_service import RunService
from backend.services.credentials_service import CredentialsService

router = APIRouter(
    prefix="/runs",
    tags=["runs"],
    responses={404: {"description": "Not found"}},
)


# =============================================================================
# DEPENDENCY INJECTION
# =============================================================================

def get_run_service(db: AsyncSession = Depends(get_db)) -> RunService:
    """Get RunService instance with injected dependencies."""
    return RunService(db)


# =============================================================================
# RESPONSE MODELS
# =============================================================================

class ExperimentRunDetailResponse(BaseModel):
    id: int
    task_id: int
    status: str
    trigger_source: Optional[str] = None
    celery_task_id: Optional[str] = None
    config_snapshot: dict = {}
    prompt_version: Optional[str] = None
    thresholds_version: Optional[str] = None
    strategy_snapshot: dict = {}
    started_at: datetime
    finished_at: Optional[datetime] = None
    error_message: Optional[str] = None
    block_reason: Optional[str] = None
    next_retry_at: Optional[datetime] = None
    checkpoint: dict = {}

    class Config:
        from_attributes = True


class RunSummaryResponse(BaseModel):
    id: int
    task_id: int
    status: str
    trigger_source: Optional[str] = None
    started_at: datetime
    finished_at: Optional[datetime] = None
    error_message: Optional[str] = None
    block_reason: Optional[str] = None
    next_retry_at: Optional[datetime] = None


class TraceStepResponse(BaseModel):
    id: int
    task_id: int
    run_id: Optional[int] = None
    step_type: str
    step_order: int
    iteration: int = 1
    input_data: dict
    output_data: dict
    duration_ms: Optional[int] = None
    status: str
    error_message: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class AlphaListItem(BaseModel):
    id: int
    alpha_id: Optional[str] = None
    task_id: Optional[int] = None
    run_id: Optional[int] = None
    expression: str
    region: Optional[str] = None
    dataset_id: Optional[str] = None
    quality_status: str
    metrics: dict = {}
    created_at: datetime

    class Config:
        from_attributes = True


class AlphaListResponse(BaseModel):
    items: List[AlphaListItem]
    total: int


class TracePageResponse(BaseModel):
    items: List[TraceStepResponse]
    total: int
    limit: int
    offset: int
    has_more: bool


# =============================================================================
# ENDPOINTS
# =============================================================================

@router.get("/{run_id}", response_model=ExperimentRunDetailResponse)
async def get_run(
    run_id: int,
    service: RunService = Depends(get_run_service),
):
    """Get experiment run details."""
    run = await service.get_run(run_id)
    
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    
    return ExperimentRunDetailResponse(
        id=run.id,
        task_id=run.task_id,
        status=run.status,
        trigger_source=run.trigger_source,
        celery_task_id=run.celery_task_id,
        config_snapshot=run.config_snapshot,
        prompt_version=run.prompt_version,
        thresholds_version=run.thresholds_version,
        strategy_snapshot=run.strategy_snapshot,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error_message=run.error_message,
        block_reason=run.block_reason,
        next_retry_at=run.next_retry_at,
        checkpoint=run.checkpoint or {},
    )


@router.get("/{run_id}/summary", response_model=RunSummaryResponse)
async def get_run_summary(
    run_id: int,
    service: RunService = Depends(get_run_service),
):
    """Run status without config, strategy, checkpoint, or Celery metadata."""
    run = await service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return RunSummaryResponse(
        id=run.id, task_id=run.task_id, status=run.status,
        trigger_source=run.trigger_source, started_at=run.started_at,
        finished_at=run.finished_at,
        error_message=CredentialsService.sanitize_error(run.error_message) if run.error_message else None,
        block_reason=CredentialsService.sanitize_error(run.block_reason) if run.block_reason else None,
        next_retry_at=run.next_retry_at,
    )


@router.get("/{run_id}/trace", response_model=List[TraceStepResponse])
async def get_run_trace(
    run_id: int,
    service: RunService = Depends(get_run_service),
):
    """Get trace steps for an experiment run."""
    try:
        steps = await service.get_run_trace(run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    
    return [
        TraceStepResponse(
            id=s.id,
            task_id=s.task_id,
            run_id=s.run_id,
            step_type=s.step_type,
            step_order=s.step_order,
            iteration=s.iteration,
            input_data=s.input_data,
            output_data=s.output_data,
            duration_ms=s.duration_ms,
            status=s.status,
            error_message=s.error_message,
            created_at=s.created_at,
        )
        for s in steps
    ]


@router.get("/{run_id}/trace-page", response_model=TracePageResponse)
async def get_run_trace_page(
    run_id: int,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    iteration: Optional[int] = Query(None, ge=1),
    step_type: Optional[str] = Query(None, max_length=50),
    service: RunService = Depends(get_run_service),
):
    """Read a bounded trace page; the original full trace endpoint remains available."""
    try:
        page = await service.get_run_trace_page(run_id, limit, offset, iteration, step_type)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return TracePageResponse(
        items=[TraceStepResponse(**vars(s)) for s in page.items],
        total=page.total, limit=limit, offset=offset,
        has_more=offset + len(page.items) < page.total,
    )


@router.get("/{run_id}/alphas", response_model=AlphaListResponse)
async def get_run_alphas(
    run_id: int,
    quality_status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    service: RunService = Depends(get_run_service),
):
    """Get alphas for an experiment run."""
    try:
        result = await service.get_run_alphas(
            run_id=run_id,
            quality_status=quality_status,
            limit=limit,
            offset=offset,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    
    items = [
        AlphaListItem(
            id=a.id,
            alpha_id=a.alpha_id,
            task_id=a.task_id,
            run_id=a.run_id,
            expression=a.expression,
            region=a.region,
            dataset_id=a.dataset_id,
            quality_status=a.quality_status,
            metrics=a.metrics,
            created_at=a.created_at,
        )
        for a in result.items
    ]
    
    return AlphaListResponse(items=items, total=result.total)
