"""Public service exports, loaded only when requested.

Importing a pure policy submodule must not initialize SQLAlchemy, configuration,
credentials or the runtime. Existing ``from backend.services import X`` callers
keep the same classes and functions through the lazy compatibility exports.
"""
from importlib import import_module


_EXPORTS = {
    'BaseService': 'backend.services.base',
    'transactional': 'backend.services.base',
    'AlphaService': 'backend.services.alpha_service',
    'AlphaListFilters': 'backend.services.alpha_service',
    'DashboardService': 'backend.services.dashboard_service',
    'MiningService': 'backend.services.mining_service',
    'TaskService': 'backend.services.task_service',
    'TaskCreateData': 'backend.services.task_service',
    'AnalysisService': 'backend.services.analysis_service',
    'CredentialsService': 'backend.services.credentials_service',
    'DatasetService': 'backend.services.dataset_service',
    'DatasetListFilters': 'backend.services.dataset_service',
    'DatasetInfo': 'backend.services.dataset_service',
    'DataFieldInfo': 'backend.services.dataset_service',
    'KnowledgeService': 'backend.services.knowledge_service',
    'KnowledgeListFilters': 'backend.services.knowledge_service',
    'KnowledgeCreateData': 'backend.services.knowledge_service',
    'KnowledgeUpdateData': 'backend.services.knowledge_service',
    'KnowledgeEntryInfo': 'backend.services.knowledge_service',
    'RunService': 'backend.services.run_service',
    'RunDetailInfo': 'backend.services.run_service',
    'TraceStepInfo': 'backend.services.run_service',
    'AlphaListItem': 'backend.services.run_service',
    'OperatorService': 'backend.services.operator_service',
    'OperatorListFilters': 'backend.services.operator_service',
    'OperatorInfo': 'backend.services.operator_service',
    'ConfigService': 'backend.services.config_service',
    'ThresholdsConfig': 'backend.services.config_service',
    'DiversityConfig': 'backend.services.config_service',
    'OperatorPrefInfo': 'backend.services.config_service',
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
