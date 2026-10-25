"""Durable lineage and simulation reservations for audited research runs."""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class ResearchCandidate(SQLAlchemyBase):
    __tablename__ = "research_candidates"
    __table_args__ = (
        Index("ix_research_candidates_run_signature", "run_id", "expression_hash", "settings_hash"),
        Index("ix_research_candidates_run_status", "run_id", "request_status"),
        Index("ix_research_candidates_task_signature", "task_id", "expression_hash"),
    )

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("mining_tasks.id"), nullable=False)
    run_id = Column(Integer, ForeignKey("experiment_runs.id"), nullable=False)
    trace_step_id = Column(Integer, ForeignKey("trace_steps.id"), nullable=True)
    parent_candidate_id = Column(Integer, ForeignKey("research_candidates.id"), nullable=True)
    family_id = Column(String(120), nullable=False)
    experiment_group = Column(String(80), nullable=True)
    arm = Column(String(20), nullable=True)
    window = Column(Integer, nullable=True)
    pair_id = Column(String(80), nullable=True)
    source = Column(String(30), nullable=False)
    expression = Column(Text, nullable=False)
    expression_hash = Column(String(64), nullable=False)
    settings_hash = Column(String(64), nullable=False)
    settings = Column(JSONB, nullable=False)
    validation_status = Column(String(30), nullable=False, default="VALID")
    rejection_stage = Column(String(40), nullable=True)
    rejection_reason = Column(String(100), nullable=True)
    request_status = Column(String(30), nullable=False, default="NOT_REQUESTED")
    wq_alpha_id = Column(String(30), nullable=True)
    quality_status = Column(String(30), nullable=True)
    gate_failures = Column(JSONB, nullable=False, default=list)
    metrics = Column(JSONB, nullable=False, default=dict)
    result_snapshot = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
