"""
Services Module - Business logic layer

This module provides service classes that encapsulate business logic,
separating it from routers (presentation) and repositories (data access).

All services inherit from BaseService and use dependency injection
for external dependencies.

Usage:
    from backend.services import AlphaService, DashboardService
    
    async def my_handler(db: AsyncSession):
        service = AlphaService(db)
        alphas, total = await service.list_alphas(filters)
"""

from backend.services.base import BaseService, transactional

# Core Services
from backend.services.alpha_service import AlphaService, AlphaListFilters
from backend.services.dashboard_service import DashboardService
from backend.services.mining_service import MiningService
from backend.services.task_service import TaskService, TaskCreateData
from backend.services.analysis_service import AnalysisService
from backend.services.credentials_service import CredentialsService

# New Services (added in refactoring)
from backend.services.dataset_service import (
    DatasetService,
    DatasetListFilters,
    DatasetInfo,
    DataFieldInfo,
)
from backend.services.knowledge_service import (
    KnowledgeService,
    KnowledgeListFilters,
    KnowledgeCreateData,
    KnowledgeUpdateData,
    KnowledgeEntryInfo,
)
from backend.services.run_service import (
    RunService,
    RunDetailInfo,
    TraceStepInfo,
    AlphaListItem,
)
from backend.services.operator_service import (
    OperatorService,
    OperatorListFilters,
    OperatorInfo,
)
from backend.services.config_service import (
    ConfigService,
    ThresholdsConfig,
    DiversityConfig,
    OperatorPrefInfo,
)

__all__ = [
    # Base
    "BaseService",
    "transactional",
    # Core Services
    "AlphaService",
    "AlphaListFilters",
    "DashboardService",
    "MiningService",
    "TaskService",
    "TaskCreateData",
    "AnalysisService",
    "CredentialsService",
    # Dataset Service
    "DatasetService",
    "DatasetListFilters",
    "DatasetInfo",
    "DataFieldInfo",
    # Knowledge Service
    "KnowledgeService",
    "KnowledgeListFilters",
    "KnowledgeCreateData",
    "KnowledgeUpdateData",
    "KnowledgeEntryInfo",
    # Run Service
    "RunService",
    "RunDetailInfo",
    "TraceStepInfo",
    "AlphaListItem",
    # Operator Service
    "OperatorService",
    "OperatorListFilters",
    "OperatorInfo",
    # Config Service
    "ConfigService",
    "ThresholdsConfig",
    "DiversityConfig",
    "OperatorPrefInfo",
]
