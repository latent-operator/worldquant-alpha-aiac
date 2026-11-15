"""Pinned local-only architecture checks with executable negative fixtures."""
from __future__ import annotations

import os
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = {'lint-imports': '2.15', 'semgrep': '1.185.0', 'ruff': '0.17.4'}
# Only the reviewed lifecycle/adapter/quality/coordinator scope is blocking.
RUFF_TARGETS = (
    'scripts/advance_theme_ppa.py', 'scripts/watch_theme_ppa.py',
    'backend/adapters/mcp_brain_adapter.py', 'backend/adapters/brain_adapter.py',
    'backend/agents/graph/nodes/evaluation.py',
    'backend/services/alpha_quality_evaluation.py', 'backend/services/alpha_quality_policy.py',
    'backend/services/theme_ppa_v2.py', 'backend/services/theme_ppa.py',
    'backend/services/research_audit.py', 'backend/services/request_lifecycle.py',
)
RUFF_OPTIONS = ('check', '--isolated', '--no-cache', '--select', 'F401,F811,F821')


def scanner_environment(source):
    return {**source, 'SEMGREP_APP_TOKEN': '', 'SEMGREP_SEND_METRICS': 'off',
            'SEMGREP_ENABLE_VERSION_CHECK': '0', 'NO_COLOR': '1', 'PYTHONUTF8': '1'}


def resolve_tool(name):
    directory = os.environ.get('AIAC_GOVERNANCE_BIN')
    executable = shutil.which(name, path=directory) if directory else shutil.which(name)
    if not executable:
        raise RuntimeError(f'Missing required tool {name}; install requirements-governance.txt in a separate environment and set AIAC_GOVERNANCE_BIN')
    return executable


def invoke(argv, *, cwd=ROOT, env=None):
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', check=False)


def verify_version(executable, expected, env):
    result = invoke([executable, '--version'], env=env)
    found = re.search(r'\b\d+\.\d+(?:\.\d+)?\b', result.stdout)
    if result.returncode or not found or found.group() != expected:
        raise RuntimeError(f'Unreviewed tool version for {Path(executable).name}; expected {expected}')


def require_success(argv, env):
    result = invoke(argv, env=env)
    print(result.stdout, end='')
    if result.returncode:
        print(result.stderr, end='')
        raise RuntimeError(f'Architecture check failed: {Path(argv[0]).name} (exit {result.returncode})')


def verify_import_fixture(executable, env):
    """A broken fixture must fail for the contract, and its repaired copy pass."""
    with tempfile.TemporaryDirectory(prefix='aiac-import-contract-') as folder:
        root = Path(folder)
        package = root / 'archfixture'
        package.mkdir()
        (package / '__init__.py').write_text('', encoding='utf-8')
        (package / 'effects.py').write_text('', encoding='utf-8')
        policy = package / 'policy.py'
        policy.write_text('import archfixture.effects\n', encoding='utf-8')
        config = root / 'contract.ini'
        config.write_text('[importlinter]\nroot_package=archfixture\n'
            '[importlinter:contract:pure]\nname=Policy fixture must stay pure\n'
            'type=forbidden\nsource_modules=archfixture.policy\n'
            'forbidden_modules=archfixture.effects\n', encoding='utf-8')
        local_env = {**env, 'PYTHONPATH': str(root)}
        argv = [executable, '--config', str(config), '--no-cache']
        broken = invoke(argv, cwd=root, env=local_env)
        if broken.returncode != 1 or '1 broken' not in broken.stdout or 'Policy fixture must stay pure' not in broken.stdout:
            raise RuntimeError('Import contract negative fixture did not detect the forbidden import')
        # Quality policy must also reject a side effect hidden behind a helper.
        helper = package / 'helper.py'
        helper.write_text('import archfixture.effects\n', encoding='utf-8')
        policy.write_text('import archfixture.helper\n', encoding='utf-8')
        indirect = invoke(argv, cwd=root, env=local_env)
        if indirect.returncode != 1 or '1 broken' not in indirect.stdout:
            raise RuntimeError('Import contract missed an indirect forbidden dependency')
        helper.write_text('VALUE = 1\n', encoding='utf-8')
        repaired = invoke(argv, cwd=root, env=local_env)
        if repaired.returncode or '1 kept' not in repaired.stdout:
            raise RuntimeError('Import contract repaired fixture did not pass')
    print('Import contract positive/negative fixtures passed.')


def verify_ruff_fixture(executable, env):
    """Check all selected rules, then prove a narrow compatibility exception works."""
    with tempfile.TemporaryDirectory(prefix='aiac-ruff-contract-') as folder:
        root = Path(folder)
        fixture = root / 'fixture.py'
        fixture.write_text('import os\n'
                           'def repeated():\n    return 1\n'
                           'def repeated():\n    return 2\n'
                           'VALUE = missing\n', encoding='utf-8')
        argv = [executable, *RUFF_OPTIONS, '--output-format', 'json', str(fixture)]
        broken = invoke(argv, cwd=root, env=env)
        try:
            codes = {item['code'] for item in json.loads(broken.stdout)}
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError('Ruff negative fixture produced no valid findings') from exc
        if broken.returncode != 1 or codes != {'F401', 'F811', 'F821'}:
            raise RuntimeError('Ruff negative fixture did not detect every selected violation')
        fixture.write_text('import os  # noqa: F401 - compatibility export fixture\n'
                           'def repeated():\n    return 2\n'
                           'VALUE = repeated()\n', encoding='utf-8')
        repaired = invoke(argv, cwd=root, env=env)
        if repaired.returncode or repaired.stdout.strip() != '[]':
            raise RuntimeError('Ruff repaired fixture did not pass')
    print('Ruff F401/F811/F821 positive/negative fixtures passed.')


def main():
    # Windows terminals may use GBK while scanner output contains Unicode glyphs.
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    env = scanner_environment(dict(os.environ))
    tools = {name: resolve_tool(name) for name in VERSIONS}
    for name, executable in tools.items():
        verify_version(executable, VERSIONS[name], env)
    require_success([tools['lint-imports'], '--config', '.importlinter', '--no-cache'], env)
    verify_import_fixture(tools['lint-imports'], env)
    require_success([tools['semgrep'], 'scan', '--test', '--config', '.semgrep/request-boundaries.yml',
                     '.semgrep/request-boundaries.py', '--metrics=off', '--disable-version-check'], env)
    require_success([tools['semgrep'], 'scan', '--config', '.semgrep/request-boundaries.yml',
                     '--metrics=off', '--disable-version-check', '--error', '--strict',
                     '--exclude', 'backend/tests', 'backend', 'scripts'], env)
    verify_ruff_fixture(tools['ruff'], env)
    require_success([tools['ruff'], *RUFF_OPTIONS, '--output-format', 'concise', *RUFF_TARGETS], env)
    print('AIAC architecture boundaries passed; tools used local rules only.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
