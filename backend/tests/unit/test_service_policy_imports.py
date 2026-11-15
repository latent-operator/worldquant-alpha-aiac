"""Package initialization must not turn a pure policy import into runtime startup."""
import importlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.preflight import offline_environment


@pytest.mark.parametrize('module', ['alpha_quality_policy', 'request_lifecycle'])
def test_policy_import_in_fresh_process_cannot_load_runtime_dependencies(module):
    script = '''
import importlib, importlib.abc, sys
forbidden = ('backend.database', 'backend.models', 'backend.config', 'backend.adapters',
             'backend.agents', 'sqlalchemy', 'httpx', 'requests', 'aiohttp')
class RejectRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in forbidden):
            raise AssertionError('Pure policy attempted runtime import: ' + fullname)
sys.meta_path.insert(0, RejectRuntime())
importlib.import_module(sys.argv[1])
assert not any(name in sys.modules for name in forbidden)
'''
    result = subprocess.run([sys.executable, '-c', script, 'backend.services.' + module],
        cwd=Path(__file__).parents[3], env=offline_environment(os.environ),
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    assert result.returncode == 0, result.stderr


def test_existing_service_exports_keep_their_public_identity():
    import backend.services as services
    expected = {'BaseService', 'transactional', 'AlphaService', 'AlphaListFilters',
        'DashboardService', 'MiningService', 'TaskService', 'TaskCreateData', 'AnalysisService',
        'CredentialsService', 'DatasetService', 'DatasetListFilters', 'DatasetInfo', 'DataFieldInfo',
        'KnowledgeService', 'KnowledgeListFilters', 'KnowledgeCreateData', 'KnowledgeUpdateData',
        'KnowledgeEntryInfo', 'RunService', 'RunDetailInfo', 'TraceStepInfo', 'AlphaListItem',
        'OperatorService', 'OperatorListFilters', 'OperatorInfo', 'ConfigService',
        'ThresholdsConfig', 'DiversityConfig', 'OperatorPrefInfo'}
    assert set(services.__all__) == expected
    assert expected <= set(dir(services))
    for name in expected:
        exported = getattr(services, name)
        assert exported is getattr(importlib.import_module(exported.__module__), name)
    from backend.services import AlphaService, BaseService, ConfigService
    assert (AlphaService, BaseService, ConfigService) == (
        services.AlphaService, services.BaseService, services.ConfigService)
    with pytest.raises(AttributeError, match='not_a_service'):
        getattr(services, 'not_a_service')
