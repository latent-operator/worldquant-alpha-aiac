"""A software deployment resumes only the exact runs it paused."""

import pytest

from scripts import release_tasks


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
