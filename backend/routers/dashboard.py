"""
Dashboard Router - Stats and live feed for the control center

Uses DashboardService for all business logic.
"""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from typing import Optional, List
import asyncio
import json

from backend.database import get_db
from backend.services import DashboardService

router = APIRouter(
    prefix="/stats",
    tags=["dashboard"],
    responses={404: {"description": "Not found"}},
)


# =============================================================================
# DEPENDENCY INJECTION
# =============================================================================

def get_dashboard_service(db: AsyncSession = Depends(get_db)) -> DashboardService:
    """Get DashboardService instance with injected dependencies."""
    return DashboardService(db)


# =============================================================================
# RESPONSE MODELS
# =============================================================================

from pydantic import BaseModel


class DailyStatsResponse(BaseModel):
    date: str
    goal: int
    current: int
    success_rate: float
    avg_sharpe: float
    total_simulations: int
    total_failures: int


class TaskStatusSummary(BaseModel):
    id: int
    task_name: str
    region: str
    status: str
    progress: str
    current_step: Optional[str] = None
    current_dataset: Optional[str] = None


class KPIMetrics(BaseModel):
    today_simulations: int
    today_success_rate: float
    today_avg_sharpe: float
    week_total_alphas: int


# =============================================================================
# ENDPOINTS
# =============================================================================

@router.get("/daily", response_model=DailyStatsResponse)
async def get_daily_stats(
    date: Optional[str] = Query(None, description="Date in YYYY-MM-DD format"),
    service: DashboardService = Depends(get_dashboard_service),
):
    """
    Get daily mining statistics for the dashboard.
    """
    target_date = None
    if date:
        target_date = datetime.strptime(date, "%Y-%m-%d").date()
    
    stats = await service.get_daily_stats(target_date)
    
    return DailyStatsResponse(
        date=stats.date,
        goal=stats.goal,
        current=stats.current,
        success_rate=stats.success_rate,
        avg_sharpe=stats.avg_sharpe,
        total_simulations=stats.total_simulations,
        total_failures=stats.total_failures,
    )


@router.get("/active-tasks", response_model=List[TaskStatusSummary])
async def get_active_tasks(
    service: DashboardService = Depends(get_dashboard_service),
):
    """
    Get currently active mining tasks.
    """
    tasks = await service.get_active_tasks()
    
    return [
        TaskStatusSummary(
            id=task.id,
            task_name=task.task_name,
            region=task.region,
            status=task.status,
            progress=task.progress,
            current_step=task.current_step,
            current_dataset=task.current_dataset,
        )
        for task in tasks
    ]


@router.get("/kpi", response_model=KPIMetrics)
async def get_kpi_metrics(
    service: DashboardService = Depends(get_dashboard_service),
):
    """
    Get key performance indicators for dashboard cards.
    """
    kpi = await service.get_kpi_metrics()
    
    return KPIMetrics(
        today_simulations=kpi.today_simulations,
        today_success_rate=kpi.today_success_rate,
        today_avg_sharpe=kpi.today_avg_sharpe,
        week_total_alphas=kpi.week_total_alphas,
    )


@router.get("/live-feed")
async def live_feed(db: AsyncSession = Depends(get_db)):
    """
    Server-Sent Events endpoint for live activity feed.
    Returns the latest trace steps and alpha events.
    """
    async def event_generator():
        last_id = 0
        service = DashboardService(db)
        
        while True:
            # Fetch new trace steps since last check
            steps = await service.get_recent_trace_steps(since_id=last_id, limit=10)
            
            for step in steps:
                last_id = max(last_id, step.get("id", 0))
                yield f"data: {json.dumps(step)}\n\n"
            
            await asyncio.sleep(2)  # Poll every 2 seconds
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
