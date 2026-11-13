"""The informational governance scan must never expose finding contents."""
import importlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import yaml


def test_governance_report_entrypoint_exists():
    assert importlib.util.find_spec("scripts.governance_report") is not None


def test_report_prints_only_counts_and_enforces_redaction(monkeypatch, tmp_path, capsys):
    module = importlib.import_module("scripts.governance_report")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[0] == "ruff":
            return SimpleNamespace(returncode=1, stdout=json.dumps([{"message": "PRIVATE_FIXTURE"}]))
        if args[0] == "knip":
            return SimpleNamespace(returncode=1, stdout=json.dumps({"issues": [{"dependencies": [{"name": "PRIVATE_FIXTURE"}]}]}))
        assert "--redact=100" in args
        path = Path(args[args.index("--report-path") + 1])
        path.write_text(json.dumps([{"Secret": "REDACTED"}]), encoding="utf-8")
        return SimpleNamespace(returncode=1, stdout="PRIVATE_FIXTURE")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    result = module.run(tmp_path, ruff="ruff", knip="knip", gitleaks="gitleaks")
    assert result == {"ruff": 1, "knip": 1, "gitleaks": 1}
    assert "PRIVATE_FIXTURE" not in capsys.readouterr().out
    assert len(calls) == 3


def test_report_rejects_scanner_execution_failure(monkeypatch, tmp_path):
    module = importlib.import_module("scripts.governance_report")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=2, stdout="PRIVATE_FIXTURE"))
    try:
        module.run(tmp_path, ruff="ruff", knip="knip", gitleaks="gitleaks")
    except RuntimeError as exc:
        assert "PRIVATE_FIXTURE" not in str(exc)
    else:
        raise AssertionError("a scanner execution error must not be reported as success")


def test_ci_governance_is_informational_and_does_not_upload_results():
    workflow = Path(__file__).parents[3] / ".github" / "workflows" / "ci.yml"
    config = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    job = config["jobs"]["governance-report"]
    assert job["continue-on-error"] is True
    assert all("upload-artifact" not in step.get("uses", "") for step in job["steps"])
    assert any("scripts/governance_report.py" in step.get("run", "") for step in job["steps"])
