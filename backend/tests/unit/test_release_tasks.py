"""A software deployment resumes only the exact runs it paused."""

import pytest
import os
import subprocess
import sys

from scripts import release_tasks


def test_release_cli_loads_explicit_environment_before_database(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("CREDENTIALS_SECRET=offline-test\nPOSTGRES_PORT=12345\n", encoding="utf-8")
    code = """import os, sys
import scripts.release_tasks as r
assert 'backend.database' not in sys.modules
async def fake(args):
    assert os.environ['POSTGRES_PORT'] == '12345'
r._run = fake
r.main(['capture', '--env-file', sys.argv[1]])
"""
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP") if key in os.environ}
    env.update(CREDENTIALS_SECRET="", POSTGRES_PORT="1")
    result = subprocess.run([sys.executable, "-c", code, str(env_file)], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-400:]


def test_active_entries_exclude_prior_paused_and_stopped_tasks():
    snapshot = {"tasks": [
        {"task_id": 9, "task_status": "PAUSED", "run_id": 12},
        {"task_id": 10, "task_status": "RUNNING", "run_id": 13},
        {"task_id": 4, "task_status": "STOPPED", "run_id": 6},
    ]}
    assert release_tasks.active_entries(snapshot) == [
        {"task_id": 10, "task_status": "RUNNING", "run_id": 13},
    ]


def test_resume_rejects_changed_run_identity():
    before = {"tasks": [{"task_id": 10, "task_status": "RUNNING", "run_id": 13}]}
    after = {"tasks": [{"task_id": 10, "task_status": "PAUSED", "run_id": 14}]}
    with pytest.raises(ValueError, match="run ID"):
        release_tasks.validate_resume_target(before, after)


def test_result_count_must_not_fall_after_release():
    before = {"alpha_count": 100, "tasks": [{"task_id": 10, "run_id": 13}]}
    after = {"alpha_count": 99, "tasks": [{"task_id": 10, "run_id": 13}]}
    with pytest.raises(ValueError, match="Alpha"):
        release_tasks.validate_persistent_state(before, after)
