"""Deployment must not terminate an unresponsive solo worker mid-round."""

from scripts import wait_worker_idle


def test_idle_requires_a_worker_reply_with_no_active_tasks():
    assert wait_worker_idle.is_idle(None) is False
    assert wait_worker_idle.is_idle({}) is False
    assert wait_worker_idle.is_idle({"worker@local": [{"id": "running"}]}) is False
    assert wait_worker_idle.is_idle({"worker@local": []}) is True
