"""Clean reinstall keeps results without making legacy runs executable."""
import pytest

from scripts.clean_install import UPSTREAM_COMMIT, project_row, validate_source


def test_legacy_task_tables_cannot_enter_active_database():
    with pytest.raises(ValueError, match="archive only"):
        project_row("mining_tasks", {"id": 14}, {"id"})


def test_alpha_identity_metrics_and_lineage_are_preserved_separately():
    source = {"id": 7, "alpha_id": "abc", "task_id": 14, "run_id": 28,
              "trace_step_id": 3, "is_sharpe": 0.4, "quality_status": "FAIL"}
    row, lineage = project_row("alphas", source, set(source))
    assert row["id"] == 7 and row["alpha_id"] == "abc"
    assert row["is_sharpe"] == 0.4 and row["quality_status"] == "FAIL"
    assert row["task_id"] is None and row["run_id"] is None
    assert lineage == {"table": "alphas", "id": 7, "task_id": 14,
                       "run_id": 28, "trace_step_id": 3}
    assert source["task_id"] == 14


def test_unknown_tables_and_auth_sessions_are_archive_only():
    for table in ("research_candidates", "brain_auth_tokens", "mining_jobs"):
        with pytest.raises(ValueError, match="archive only"):
            project_row(table, {}, set())


def test_projection_keeps_encrypted_config_unchanged():
    row, lineage = project_row("system_configs", {"id": 1, "config_value": "cipher"},
                               {"id", "config_value"})
    assert row["config_value"] == "cipher" and lineage is None


def test_release_requires_pinned_clean_upstream(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.clean_install.git_output", lambda *_: "wrong")
    with pytest.raises(ValueError, match="upstream"):
        validate_source(tmp_path)
    monkeypatch.setattr("scripts.clean_install.git_output",
                        lambda path, *args: UPSTREAM_COMMIT if args[0] == "rev-parse" else "")
    validate_source(tmp_path)
