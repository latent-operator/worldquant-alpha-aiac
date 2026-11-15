import importlib
import importlib.util
import json
from pathlib import Path

import pytest


def cli():
    assert importlib.util.find_spec('scripts.ppa_isolation'), 'Isolation release CLI missing'
    return importlib.import_module('scripts.ppa_isolation')


def test_scope_is_only_the_authorized_run():
    cli().require_scope({'task_id': 14, 'run_id': 28, 'commit': 'a'*40}, 'a'*40)
    for patch in ({'task_id': 15}, {'run_id': 29}, {'commit': 'b'*40}):
        with pytest.raises(ValueError, match='PPA_ISOLATION_SCOPE'):
            cli().require_scope({'task_id': 14, 'run_id': 28, 'commit': 'a'*40, **patch}, 'a'*40)


def test_private_evidence_requires_existing_absolute_paths_and_no_overwrite(tmp_path):
    path = tmp_path / 'proof.json'
    path.write_text('{}')
    assert len(cli().evidence_hashes([path])[0]) == 64
    with pytest.raises(ValueError):
        cli().evidence_hashes([tmp_path / 'missing'])
    with pytest.raises(ValueError):
        cli().private_path(cli().ROOT / 'proof.json')
    with pytest.raises(FileExistsError):
        cli().write_new(path, {'changed': True})
    assert json.loads(path.read_text()) == {}


def test_apply_requires_maintenance_and_new_journal_before_db_access(tmp_path, monkeypatch):
    module = cli()
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'task_id': 14, 'run_id': 28, 'commit': 'a'*40}))
    from scripts import verify_release
    monkeypatch.setattr(verify_release, 'verify', lambda *args: {'ci_url': 'offline-test'})
    with pytest.raises(ValueError, match='PPA_MAINTENANCE_REQUIRED'):
        module.main(['apply', '--manifest', str(manifest), '--env-file', str(tmp_path/'private.env'),
            '--commit', 'a'*40, '--ci-run', '123', '--current-file', str(tmp_path/'current.json'),
            '--maintenance-file', str(tmp_path/'maintenance.lock'), '--journal', str(tmp_path/'journal.json')])
    assert not (tmp_path/'journal.json').exists()


def test_release_isolation_is_checked_before_pause_and_applied_after_restore():
    script = (Path(__file__).resolve().parents[3] / 'scripts/deploy-local.ps1').read_text(encoding='utf-8-sig')
    assert 'scripts.ppa_isolation check' in script
    assert script.index('scripts.ppa_isolation check') < script.index('scripts.release_tasks pause')
    assert script.index('scripts.ppa_isolation apply') > script.index("Assert-Native 'Isolated database restore'")
    assert script.index('scripts.ppa_isolation apply') < script.index('scripts.run_migrations')
    assert script.index("Assert-Native 'Post-release isolation hold'") > script.index('Verify-Services $Commit', script.index('scripts.run_migrations'))


@pytest.mark.parametrize('failure', [None, 'database', 'environment'])
def test_apply_preserves_audit_journal_on_success_or_uncertain_failure(tmp_path, monkeypatch, failure):
    module = cli()
    from scripts import launch_runtime, verify_release
    monkeypatch.setattr(verify_release, 'verify', lambda *args: {'ci_url': 'offline-test'})
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'task_id': 14, 'run_id': 28, 'commit': 'a'*40,
                                   'manifest_sha256': 'b'*64}))
    (tmp_path / 'current.json').write_text(json.dumps({'commit': 'c'*40}))
    (tmp_path / 'maintenance.lock').touch()
    def load(_path):
        if failure == 'environment':
            raise RuntimeError('offline environment error')
    monkeypatch.setattr(launch_runtime, 'load_runtime_env', load)
    async def execute(action, value, commit, hashes):
        assert action == 'apply' and value['run_id'] == 28
        if failure == 'database':
            raise RuntimeError('offline database error')
        return {'outcome': 'ISOLATED_INCOMPLETE', 'blocked_signature_count': 16}
    monkeypatch.setattr(module, 'execute', execute)
    argv = ['apply', '--manifest', str(manifest), '--env-file', str(tmp_path/'private.env'),
            '--commit', 'a'*40, '--ci-run', '123', '--current-file', str(tmp_path/'current.json'),
            '--maintenance-file', str(tmp_path/'maintenance.lock'), '--journal', str(tmp_path/'journal.json')]
    if failure:
        with pytest.raises(RuntimeError):
            module.main(argv)
    else:
        module.main(argv)
    journal = json.loads((tmp_path/'journal.json').read_text())
    assert journal['outcome'] == ('FAILED_OR_UNCERTAIN' if failure else 'ISOLATED_INCOMPLETE')
    assert journal['manifest_sha256'] == 'b'*64
    assert 'offline database error' not in json.dumps(journal)
    with pytest.raises(FileExistsError):
        module.main(argv)
