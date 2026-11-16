"""
Alphas Router - Alpha Lab functionality with feedback support

Uses AlphaService for all business logic.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime

from backend.database import get_db
from backend.services import AlphaService, AlphaListFilters
from backend.tasks import sync_user_alphas

router = APIRouter(
    prefix="/alphas",
    tags=["alphas"],
    responses={404: {"description": "Not found"}},
)


# =============================================================================
# DEPENDENCY INJECTION
# =============================================================================

def get_alpha_service(db: AsyncSession = Depends(get_db)) -> AlphaService:
    """Get AlphaService instance with injected dependencies."""
    return AlphaService(db)


# =============================================================================
# REQUEST/RESPONSE MODELS
# =============================================================================

class AlphaListItem(BaseModel):
    id: int
    alpha_id: Optional[str] = None
    type: Optional[str] = "REGULAR"
    name: Optional[str] = None
    expression: str
    region: Optional[str] = None
    dataset_id: Optional[str] = None
    quality_status: str
    human_feedback: str
    sharpe: Optional[float] = None
    returns: Optional[float] = None
    turnover: Optional[float] = None
    drawdown: Optional[float] = None
    margin: Optional[float] = None
    fitness: Optional[float] = None
    created_at: Optional[datetime] = None
    date_created: Optional[datetime] = None
    
    class Config:
        from_attributes = True


class AlphaDetailResponse(BaseModel):
    id: int
    alpha_id: Optional[str] = None
    task_id: Optional[int] = None
    expression: str
    hypothesis: Optional[str] = None
    logic_explanation: Optional[str] = None
    
    # Metadata
    region: Optional[str] = None
    universe: Optional[str] = None
    dataset_id: Optional[str] = None
    fields_used: List[str] = []
    operators_used: List[str] = []
    
    # Status
    status: str = "created"
    quality_status: str = "PENDING"
    human_feedback: str = "NONE"
    feedback_comment: Optional[str] = None
    
    # Metrics
    metrics: dict = {}
    is_metrics: dict = {}
    os_metrics: dict = {}
    
    created_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True


class FeedbackRequest(BaseModel):
    rating: str  # LIKED or DISLIKED
    comment: Optional[str] = None


class SyncResponse(BaseModel):
    message: str
    task_id: str


class AlphaListResponse(BaseModel):
    items: List[AlphaListItem]
    total: int


# =============================================================================
# ENDPOINTS
# =============================================================================

@router.post("/sync", response_model=SyncResponse)
async def sync_alphas(background_tasks: BackgroundTasks = None):
    """
    Trigger background sync of ALL user alphas from Brain.
    Includes IS and OS stages, with full metadata.
    """
    task = sync_user_alphas.delay()
    return SyncResponse(
        message="Alpha sync started",
        task_id=str(task.id)
    )


@router.get("", response_model=AlphaListResponse)
async def list_alphas(
    region: Optional[str] = Query(None),
    quality_status: Optional[str] = Query(None),
    human_feedback: Optional[str] = Query(None),
    dataset_id: Optional[str] = Query(None),
    sort_by: str = Query("date_created", description="Field to sort by"),
    sort_order: str = Query("desc", description="asc or desc"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    service: AlphaService = Depends(get_alpha_service),
):
    """
    List alphas with filtering and sorting.
    """
    filters = AlphaListFilters(
        region=region,
        quality_status=quality_status,
        human_feedback=human_feedback,
        dataset_id=dataset_id,
    )
    
    items, total = await service.list_alphas(
        filters=filters,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
    )
    
    # Convert to response model
    response_items = [
        AlphaListItem(
            id=item.id,
            alpha_id=item.alpha_id,
            type=item.type,
            name=item.name,
            expression=item.expression,
            region=item.region,
            dataset_id=item.dataset_id,
            quality_status=item.quality_status,
            human_feedback=item.human_feedback,
            sharpe=item.sharpe,
            returns=item.returns,
            turnover=item.turnover,
            drawdown=item.drawdown,
            margin=item.margin,
            fitness=item.fitness,
            created_at=item.created_at,
        )
        for item in items
    ]
    
    return AlphaListResponse(items=response_items, total=total)


@router.get("/{alpha_id}", response_model=AlphaDetailResponse)
async def get_alpha(
    alpha_id: int,
    service: AlphaService = Depends(get_alpha_service),
):
    """
    Get detailed information about an alpha.
    """
    alpha = await service.get_alpha(alpha_id)
    
    if not alpha:
        raise HTTPException(status_code=404, detail="Alpha not found")
    
    return AlphaDetailResponse(
        id=alpha.id,
        alpha_id=alpha.alpha_id,
        task_id=alpha.task_id,
        expression=alpha.expression,
        hypothesis=alpha.hypothesis,
        logic_explanation=alpha.logic_explanation,
        region=alpha.region,
        universe=alpha.universe,
        dataset_id=alpha.dataset_id,
        fields_used=alpha.fields_used,
        operators_used=alpha.operators_used,
        status=alpha.status,
        quality_status=alpha.quality_status,
        human_feedback=alpha.human_feedback,
        feedback_comment=alpha.feedback_comment,
        metrics=alpha.metrics,
        is_metrics=alpha.is_metrics,
        os_metrics=alpha.os_metrics,
        created_at=alpha.created_at,
    )


@router.post("/{alpha_id}/feedback")
async def submit_feedback(
    alpha_id: int,
    request: FeedbackRequest,
    service: AlphaService = Depends(get_alpha_service),
):
    """
    Submit human feedback for an alpha (Human-in-the-Loop).
    This feedback is used by the Feedback Agent to improve future mining.
    """
    if request.rating not in ["LIKED", "DISLIKED"]:
        raise HTTPException(status_code=400, detail="Rating must be LIKED or DISLIKED")
    
    success = await service.submit_feedback(
        alpha_id=alpha_id,
        rating=request.rating,
        comment=request.comment,
    )
    
    if not success:
        raise HTTPException(status_code=404, detail="Alpha not found")
    
    return {
        "message": "Feedback submitted",
        "alpha_id": alpha_id,
        "rating": request.rating,
    }


@router.get("/{alpha_id}/trace", response_model=dict)
async def get_alpha_trace(
    alpha_id: int,
    service: AlphaService = Depends(get_alpha_service),
):
    """
    Get the trace step that generated this alpha.
    Shows the full context: RAG query, hypothesis, code generation, etc.
    """
    trace = await service.get_alpha_trace(alpha_id)
    
    if trace is None:
        raise HTTPException(status_code=404, detail="Alpha not found")
    
    return trace


@router.get("/by-brain-id/{brain_alpha_id}", response_model=AlphaDetailResponse)
async def get_alpha_by_brain_id(
    brain_alpha_id: str,
    service: AlphaService = Depends(get_alpha_service),
):
    """
    Get an alpha by its BRAIN platform ID.
    """
    alpha = await service.get_alpha_by_brain_id(brain_alpha_id)
    
    if not alpha:
        raise HTTPException(status_code=404, detail="Alpha not found")
    
    return AlphaDetailResponse(
        id=alpha.id,
        alpha_id=alpha.alpha_id,
        task_id=alpha.task_id,
        expression=alpha.expression,
        hypothesis=alpha.hypothesis,
        logic_explanation=alpha.logic_explanation,
        region=alpha.region,
        universe=alpha.universe,
        dataset_id=alpha.dataset_id,
        fields_used=alpha.fields_used,
        operators_used=alpha.operators_used,
        status=alpha.status,
        quality_status=alpha.quality_status,
        human_feedback=alpha.human_feedback,
        feedback_comment=alpha.feedback_comment,
        metrics=alpha.metrics,
        is_metrics=alpha.is_metrics,
        os_metrics=alpha.os_metrics,
        created_at=alpha.created_at,
    )
