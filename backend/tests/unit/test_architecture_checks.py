"""Architecture enforcement fails closed without the reviewed tool versions."""
import importlib
from types import SimpleNamespace

import pytest


def module():
    return importlib.import_module('scripts.check_architecture')


def test_missing_scanner_does_not_count_as_pass(monkeypatch):
    check = module()
    monkeypatch.delenv('AIAC_GOVERNANCE_BIN', raising=False)
    monkeypatch.setattr(check.shutil, 'which', lambda _name: None)
    with pytest.raises(RuntimeError, match='required tool'):
        check.resolve_tool('semgrep')


def test_unreviewed_tool_version_is_rejected(monkeypatch):
    check = module()
    monkeypatch.setattr(check.subprocess, 'run', lambda *a, **k:
        SimpleNamespace(returncode=0, stdout='1.185.1\n', stderr=''))
    with pytest.raises(RuntimeError, match='version'):
        check.verify_version('semgrep', '1.185.0', {})


def test_local_scanner_environment_disables_remote_configuration():
    env = module().scanner_environment({'SEMGREP_APP_TOKEN': 'private', 'SEMGREP_SEND_METRICS': 'on'})
    assert env['SEMGREP_APP_TOKEN'] == ''
    assert env['SEMGREP_SEND_METRICS'] == 'off'
    assert env['SEMGREP_ENABLE_VERSION_CHECK'] == '0'
