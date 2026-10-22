"""Released code uses the one private local environment file."""

import pytest

from scripts import launch_runtime


def test_release_source_requires_backend_and_built_ui(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "main.py").write_text("app = object()")
    (tmp_path / "frontend" / "dist").mkdir(parents=True)
    with pytest.raises(ValueError, match="index.html"):
        launch_runtime.validate_source(tmp_path)
    (tmp_path / "frontend" / "dist" / "index.html").write_text("ready")
    launch_runtime.validate_source(tmp_path)


def test_env_loader_requires_stable_encryption_secret(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("CREDENTIALS_SECRET=local-test-secret\n")
    monkeypatch.delenv("CREDENTIALS_SECRET", raising=False)
    launch_runtime.load_runtime_env(env_file)
    assert launch_runtime.os.environ["CREDENTIALS_SECRET"] == "local-test-secret"
    env_file.write_text("CREDENTIALS_SECRET=\n")
    with pytest.raises(ValueError, match="CREDENTIALS_SECRET"):
        launch_runtime.load_runtime_env(env_file)
    monkeypatch.delenv("CREDENTIALS_SECRET", raising=False)
