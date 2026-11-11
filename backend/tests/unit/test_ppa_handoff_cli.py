import pytest

from scripts import ppa_handoff


def test_install_requires_exact_current_release():
    record = {"commit": "a" * 40, "ci_run": 42}
    ppa_handoff.require_current_release(record, "a" * 40, 42)
    with pytest.raises(ValueError, match="PPA_RELEASE_MISMATCH"):
        ppa_handoff.require_current_release(record, "b" * 40, 42)
    with pytest.raises(ValueError, match="PPA_RELEASE_MISMATCH"):
        ppa_handoff.require_current_release(record, "a" * 40, 43)


def test_close_requires_actual_release_maintenance_lock(tmp_path):
    current = tmp_path / "current.json"
    lock = tmp_path / "maintenance.lock"
    with pytest.raises(ValueError, match="PPA_MAINTENANCE_REQUIRED"):
        ppa_handoff.require_maintenance(current, lock)
    lock.touch()
    ppa_handoff.require_maintenance(current, lock)
    with pytest.raises(ValueError, match="PPA_MAINTENANCE_REQUIRED"):
        ppa_handoff.require_maintenance(current, tmp_path / "wrong.lock")
