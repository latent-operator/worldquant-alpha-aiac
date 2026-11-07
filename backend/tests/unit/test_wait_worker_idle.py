"""Deployment must not terminate an unresponsive solo worker mid-round."""

from scripts import wait_worker_idle
import os


def test_idle_requires_a_worker_reply_with_no_active_tasks():
    assert wait_worker_idle.is_idle(None) is False
    assert wait_worker_idle.is_idle({}) is False
    assert wait_worker_idle.is_idle({"worker@local": [{"id": "running"}]}) is False
    assert wait_worker_idle.is_idle({"worker@local": []}) is True


def test_idle_check_uses_private_runtime_redis_url(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("REDIS_URL=redis://127.0.0.1:56979/0\n", encoding="utf-8")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:56579/0")
    wait_worker_idle.load_runtime_env(env_file)
    assert os.environ["REDIS_URL"] == "redis://127.0.0.1:56979/0"
