"""Candidate lineage and durable per-expression WQ request reservations."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_research_candidate_audit"
down_revision = "0002_llm_wait_checkpoint"
branch_labels = None
depends_on = None


def upgrade():
    if "research_candidates" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "research_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("mining_tasks.id"), nullable=False),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("experiment_runs.id"), nullable=False),
        sa.Column("trace_step_id", sa.Integer(), sa.ForeignKey("trace_steps.id")),
        sa.Column("parent_candidate_id", sa.Integer(), sa.ForeignKey("research_candidates.id")),
        sa.Column("family_id", sa.String(120), nullable=False),
        sa.Column("experiment_group", sa.String(80)),
        sa.Column("arm", sa.String(20)),
        sa.Column("window", sa.Integer()),
        sa.Column("pair_id", sa.String(80)),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("expression", sa.Text(), nullable=False),
        sa.Column("expression_hash", sa.String(64), nullable=False),
        sa.Column("settings_hash", sa.String(64), nullable=False),
        sa.Column("settings", JSONB(), nullable=False),
        sa.Column("validation_status", sa.String(30), nullable=False),
        sa.Column("rejection_stage", sa.String(40)),
        sa.Column("rejection_reason", sa.String(100)),
        sa.Column("request_status", sa.String(30), nullable=False),
        sa.Column("wq_alpha_id", sa.String(30)),
        sa.Column("quality_status", sa.String(30)),
        sa.Column("gate_failures", JSONB(), nullable=False),
        sa.Column("metrics", JSONB(), nullable=False),
        sa.Column("result_snapshot", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_research_candidates_run_signature", "research_candidates", ["run_id", "expression_hash", "settings_hash"])
    op.create_index("ix_research_candidates_run_status", "research_candidates", ["run_id", "request_status"])


def downgrade():
    op.drop_index("ix_research_candidates_run_status", table_name="research_candidates")
    op.drop_index("ix_research_candidates_run_signature", table_name="research_candidates")
    op.drop_table("research_candidates")
