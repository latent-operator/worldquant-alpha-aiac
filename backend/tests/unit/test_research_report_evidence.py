"""Derived evidence does not rewrite persisted request status counts."""
import pytest
from backend.models import MiningTask, ExperimentRun, ResearchCandidate
from backend.services.research_report import research_report, candidate_public


@pytest.mark.asyncio
async def test_report_keeps_failed_count_and_exposes_evidence_pending(db_session):
    task = MiningTask(task_name="evidence-test", region="USA", universe="TOP3000",
                      status="PAUSED", config={})
    db_session.add(task)
    await db_session.flush()
    run = ExperimentRun(task_id=task.id, status="PAUSED", config_snapshot={})
    db_session.add(run)
    await db_session.flush()
    for i, snapshot in enumerate([{"http_status": 400},
            {"request_phase": "POLL_ORIGINAL", "simulation_status": "ERROR"}]):
        row = ResearchCandidate(task_id=task.id, run_id=run.id, family_id="test",
            source="GENERATION", expression="rank(a)", expression_hash=str(i),
            settings_hash="s", settings={}, request_status="FAILED", result_snapshot=snapshot)
        db_session.add(row)
    await db_session.commit()
    report = await research_report(db_session, run.id)
    assert report["request_counts"] == {"FAILED": 2}
    assert report["evidence_pending_count"] == report["ambiguous_failed_count"] == 1
    assert report["evidence_pending_reasons"] == {"FAILED:UNKNOWN": 1}
    assert candidate_public(row)["simulation_status"] == "ERROR"
