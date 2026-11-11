"""Released code uses the one private local environment file."""

import pytest
import json

from scripts import launch_runtime


def test_coordinator_activation_is_bound_to_task_run_and_release(tmp_path):
    script = tmp_path / "cockpit.ps1"
    script.write_text("# offline", encoding="utf-8")
    config = tmp_path / "coordinator.json"
    value = {"enabled": True, "task_id": 14, "run_id": 25,
             "commit": "a" * 40, "cockpit_script": str(script), "output_dir": str(tmp_path / "out")}
    config.write_text(json.dumps(value), encoding="utf-8")
    assert launch_runtime.load_coordinator_config(config, "a" * 40)["run_id"] == 25
    with pytest.raises(ValueError, match="PPA_COORDINATOR_ACTIVATION_INVALID"):
        launch_runtime.load_coordinator_config(config, "b" * 40)
    value["task_id"] = 15
    config.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="PPA_COORDINATOR_ACTIVATION_INVALID"):
        launch_runtime.load_coordinator_config(config, "a" * 40)


def test_coordinator_entrypoint_loads_from_released_source(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "watch_theme_ppa.py").write_text(
        "def main(args):\n    return ('released', args)\n", encoding="utf-8")
    main = launch_runtime.load_coordinator_main(tmp_path)
    assert main(["--test"]) == ("released", ["--test"])


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
