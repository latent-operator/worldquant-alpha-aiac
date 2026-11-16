"""
Dashboard Service - Business logic for dashboard metrics

Provides methods for:
- Daily statistics
- KPI metrics
- Active task status
- Live feed data
"""

import logging
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta, date
from dataclasses import dataclass
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from backend.services.base import BaseService
from backend.repositories import TaskRepository
from backend.models import MiningTask, Alpha, TraceStep, AlphaFailure

logger = logging.getLogger("services.dashboard")


@dataclass
class DailyStats:
    """Daily mining statistics."""
    date: str
    goal: int
    current: int
    success_rate: float
    avg_sharpe: float
    total_simulations: int
    total_failures: int


@dataclass
class TaskStatusSummary:
    """Summary of a task's status."""
    id: int
    task_name: str
    region: str
    status: str
    progress: str
    current_step: Optional[str]
    current_dataset: Optional[str]


@dataclass
class KPIMetrics:
    """Key performance indicators."""
    today_simulations: int
    today_success_rate: float
    today_avg_sharpe: float
    week_total_alphas: int


class DashboardService(BaseService):
    """
    Service for dashboard-related operations.
    
    Provides aggregated statistics and metrics for the control center.
    """
    
    def __init__(self, db: AsyncSession):
        super().__init__(db)
        self.task_repo = TaskRepository(db)
    
    # =========================================================================
    # Daily Statistics
    # =========================================================================
    
    async def get_daily_stats(self, target_date: Optional[date] = None) -> DailyStats:
        """
        Get daily mining statistics.
        
        Args:
            target_date: Date to get stats for (defaults to today)
            
        Returns:
            DailyStats dataclass
        """
        if target_date is None:
            target_date = datetime.now().date()
        
        start_of_day = datetime.combine(target_date, datetime.min.time())
        end_of_day = start_of_day + timedelta(days=1)
        
        # Get today's tasks
        tasks_query = select(MiningTask).where(
            MiningTask.created_at >= start_of_day,
            MiningTask.created_at < end_of_day,
        )
        tasks_result = await self.db.execute(tasks_query)
        tasks = tasks_result.scalars().all()
        
        total_goal = sum(t.daily_goal for t in tasks) if tasks else 4
        total_current = sum(t.progress_current for t in tasks) if tasks else 0
        
        # Get today's passed alphas
        alphas_query = select(Alpha).where(
            Alpha.created_at >= start_of_day,
            Alpha.created_at < end_of_day,
            Alpha.quality_status == "PASS",
        )
        alphas_result = await self.db.execute(alphas_query)
        alphas = alphas_result.scalars().all()
        
        # Calculate metrics
        total_sims = await self._count_alphas_in_range(start_of_day, end_of_day)
        total_failures = await self._count_failures_in_range(start_of_day, end_of_day)
        
        success_rate = len(alphas) / total_sims if total_sims > 0 else 0.0
        avg_sharpe = self._calculate_avg_sharpe(alphas)
        
        return DailyStats(
            date=target_date.isoformat(),
            goal=total_goal,
            current=total_current,
            success_rate=round(success_rate, 4),
            avg_sharpe=round(avg_sharpe, 2),
            total_simulations=total_sims,
            total_failures=total_failures,
        )
    
    async def _count_alphas_in_range(
        self,
        start: datetime,
        end: datetime,
    ) -> int:
        """Count alphas created in a time range."""
        query = select(func.count(Alpha.id)).where(
            Alpha.created_at >= start,
            Alpha.created_at < end,
        )
        result = await self.db.execute(query)
        return result.scalar() or 0
    
    async def _count_failures_in_range(
        self,
        start: datetime,
        end: datetime,
    ) -> int:
        """Count failures in a time range."""
        query = select(func.count(AlphaFailure.id)).where(
            AlphaFailure.created_at >= start,
            AlphaFailure.created_at < end,
        )
        result = await self.db.execute(query)
        return result.scalar() or 0
    
    def _calculate_avg_sharpe(self, alphas: List[Alpha]) -> float:
        """Calculate average Sharpe ratio from alphas."""
        if not alphas:
            return 0.0
        
        sharpes = []
        for a in alphas:
            sharpe = None
            if a.metrics:
                sharpe = a.metrics.get("sharpe") or a.metrics.get("is_sharpe")
            if sharpe is None:
                sharpe = a.is_sharpe
            if sharpe is not None:
                sharpes.append(sharpe)
        
        return sum(sharpes) / len(sharpes) if sharpes else 0.0
    
    # =========================================================================
    # Active Tasks
    # =========================================================================
    
    async def get_active_tasks(self) -> List[TaskStatusSummary]:
        """
        Get currently active mining tasks with status summary.
        
        Returns:
            List of TaskStatusSummary
        """
        query = (
            select(MiningTask)
            .where(MiningTask.status.in_(["RUNNING", "PAUSED"]))
            .order_by(MiningTask.created_at.desc())
        )
        
        result = await self.db.execute(query)
        tasks = result.scalars().all()
        
        summaries = []
        for task in tasks:
            # Get the latest trace step
            step_query = (
                select(TraceStep)
                .where(TraceStep.task_id == task.id)
                .order_by(TraceStep.step_order.desc())
                .limit(1)
            )
            step_result = await self.db.execute(step_query)
            latest_step = step_result.scalar_one_or_none()
            
            current_step = latest_step.step_type if latest_step else None
            current_dataset = None
            if latest_step and latest_step.input_data:
                current_dataset = latest_step.input_data.get("dataset_id")
            
            summaries.append(
                TaskStatusSummary(
                    id=task.id,
                    task_name=task.task_name,
                    region=task.region,
                    status=task.status,
                    progress=f"{task.progress_current}/{task.daily_goal}",
                    current_step=current_step,
                    current_dataset=current_dataset,
                )
            )
        
        return summaries
    
    # =========================================================================
    # KPI Metrics
    # =========================================================================
    
    async def get_kpi_metrics(self) -> KPIMetrics:
        """
        Get key performance indicators for dashboard cards.
        
        Returns:
            KPIMetrics dataclass
        """
        today = datetime.now().date()
        start_of_today = datetime.combine(today, datetime.min.time())
        start_of_week = start_of_today - timedelta(days=today.weekday())
        
        # Today's simulations
        today_sims = await self._count_alphas_in_range(
            start_of_today,
            start_of_today + timedelta(days=1),
        )
        
        # Today's passed alphas
        today_passed_query = select(func.count(Alpha.id)).where(
            Alpha.created_at >= start_of_today,
            Alpha.quality_status == "PASS",
        )
        today_passed = (await self.db.execute(today_passed_query)).scalar() or 0
        
        success_rate = today_passed / today_sims if today_sims > 0 else 0.0
        
        # Today's average Sharpe
        today_alphas_query = select(Alpha).where(
            Alpha.created_at >= start_of_today,
            Alpha.quality_status == "PASS",
        )
        today_alphas = (await self.db.execute(today_alphas_query)).scalars().all()
        avg_sharpe = self._calculate_avg_sharpe(today_alphas)
        
        # Week's total alphas
        week_total_query = select(func.count(Alpha.id)).where(
            Alpha.created_at >= start_of_week,
            Alpha.quality_status == "PASS",
        )
        week_total = (await self.db.execute(week_total_query)).scalar() or 0
        
        return KPIMetrics(
            today_simulations=today_sims,
            today_success_rate=round(success_rate, 4),
            today_avg_sharpe=round(avg_sharpe, 2),
            week_total_alphas=week_total,
        )
    
    # =========================================================================
    # Live Feed
    # =========================================================================
    
    async def get_recent_trace_steps(
        self,
        since_id: int = 0,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Get recent trace steps for live feed.
        
        Args:
            since_id: Return steps with ID greater than this
            limit: Maximum number of steps to return
            
        Returns:
            List of trace step data dicts
        """
        query = (
            select(TraceStep)
            .where(TraceStep.id > since_id)
            .order_by(TraceStep.created_at.desc())
            .limit(limit)
        )
        
        result = await self.db.execute(query)
        steps = result.scalars().all()
        
        return [
            {
                "id": step.id,
                "type": "trace_step",
                "task_id": step.task_id,
                "step_type": step.step_type,
                "status": step.status,
                "timestamp": step.created_at.isoformat() if step.created_at else None,
                "message": self._format_step_message(step),
            }
            for step in reversed(steps)  # Oldest first
        ]
    
    def _format_step_message(self, step: TraceStep) -> str:
        """Format a trace step into a human-readable message."""
        messages = {
            "RAG_QUERY": "Mining Agent 检索知识库...",
            "HYPOTHESIS": "Mining Agent 生成假设...",
            "CODE_GEN": "Mining Agent 生成 Alpha 表达式...",
            "VALIDATE": "静态校验表达式...",
            "SIMULATE": "提交 BRAIN 模拟...",
            "SELF_CORRECT": "自我修正错误表达式...",
            "EVALUATE": "Analysis Agent 评估质量...",
        }
        
        base_msg = messages.get(step.step_type, f"执行步骤: {step.step_type}")
        
        if step.status == "SUCCESS":
            return f"✅ {base_msg} 完成"
        elif step.status == "FAILED":
            return f"❌ {base_msg} 失败"
        else:
            return f"⏳ {base_msg}"
    
    # =========================================================================
    # Task Statistics
    # =========================================================================
    
    async def get_task_status_counts(self) -> Dict[str, int]:
        """
        Get count of tasks by status.
        
        Returns:
            Dict of status -> count
        """
        return await self.task_repo.get_status_counts()
    
    async def get_region_distribution(self) -> Dict[str, int]:
        """
        Get distribution of tasks by region.
        
        Returns:
            Dict of region -> count
        """
        return await self.task_repo.get_region_distribution()
