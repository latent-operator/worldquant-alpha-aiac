import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.close_research_goal import close_goal


def test_invalid_report_never_calls_beads(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"candidate_lineage": []}))
    with patch("scripts.close_research_goal.subprocess.run") as run:
        with pytest.raises(ValueError):
            close_goal(report, tmp_path / "receipt.json", "session-test")
        run.assert_not_called()


@pytest.mark.parametrize("fail_close", [False, True])
def test_closure_records_evidence_before_close_and_failure_is_not_success(tmp_path, fail_close):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"run_id": 2, "campaign_sha256": "a" * 64,
        "candidate_lineage": [{"candidate_id": 1, "request_status": "RESULT",
        "wq_alpha_id": "a", "quality_status": "PASS", "ppa_status": "PPA_READY"}]}))
    receipt = tmp_path / "receipt.json"
    def execute(argv, **kwargs):
        if argv[:3] == ["git", "status", "--porcelain"]:
            return SimpleNamespace(stdout="")
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout="b" * 40)
        if argv[0] == "git": return SimpleNamespace(stdout=str(tmp_path / ".git"))
        assert receipt.exists()
        if "close" in argv and fail_close:
            import subprocess
            raise subprocess.CalledProcessError(1, argv)
        return SimpleNamespace(stdout="")
    with patch("scripts.close_research_goal.subprocess.run", side_effect=execute):
        import subprocess
        if fail_close:
            with pytest.raises(subprocess.CalledProcessError):
                close_goal(report, receipt, "session-test")
        else:
            close_goal(report, receipt, "session-test")
    saved = json.loads(receipt.read_text())
    assert saved["closed"] is (not fail_close)
    assert saved["session_id"] == "session-test"
    assert len(saved["report_sha256"]) == 64
