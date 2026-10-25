"""Explicit continuous tasks retain their mode in API-facing summaries."""

import pytest

from backend.services.task_service import TaskCreateData, TaskService


@pytest.mark.asyncio
async def test_continuous_create_enables_audit_and_exposes_mode(db_session):
    created = await TaskService(db_session).create_task(TaskCreateData(
        name="continuous paper", region="USA", universe="TOP3000",
        dataset_strategy="SPECIFIC", target_datasets=["news17"],
        continuous_mode=True, config={"paper_reference": "arxiv:2512.19484"},
    ))
    assert created.continuous_mode is True
    stored = await TaskService(db_session).task_repo.get_by_id(created.id)
    assert stored.config["continuous_mode"] is True
    assert stored.config["audit_candidates"] is True


@pytest.mark.asyncio
async def test_finite_create_retains_existing_defaults(db_session):
    created = await TaskService(db_session).create_task(TaskCreateData(name="finite"))
    assert created.continuous_mode is False


@pytest.mark.asyncio
async def test_fixed_paper_protocol_cannot_be_made_unbounded(db_session):
    with pytest.raises(ValueError, match="Fixed 40-request"):
        await TaskService(db_session).create_task(TaskCreateData(
            name="invalid", continuous_mode=True,
            config={"research_protocol": "paper_news17_ablation_v1"},
        ))
