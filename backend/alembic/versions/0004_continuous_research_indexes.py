"""Indexes for durable cross-task WQ request deduplication."""

from alembic import op
import sqlalchemy as sa

revision = "0004_continuous_research_indexes"
down_revision = "0003_research_candidate_audit"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    candidate_indexes = {item["name"] for item in inspector.get_indexes("research_candidates")}
    if "ix_research_candidates_task_signature" not in candidate_indexes:
        op.create_index("ix_research_candidates_task_signature", "research_candidates",
                        ["task_id", "expression_hash"])
    alpha_indexes = {item["name"] for item in inspector.get_indexes("alphas")}
    if "ix_alphas_expression_hash" not in alpha_indexes:
        op.create_index("ix_alphas_expression_hash", "alphas", ["expression_hash"])
    if "ix_alphas_run_id" not in alpha_indexes:
        op.create_index("ix_alphas_run_id", "alphas", ["run_id"])
    trace_indexes = {item["name"] for item in inspector.get_indexes("trace_steps")}
    if "ix_trace_steps_run_id_id" not in trace_indexes:
        op.create_index("ix_trace_steps_run_id_id", "trace_steps", ["run_id", "id"])


def downgrade():
    op.drop_index("ix_trace_steps_run_id_id", table_name="trace_steps")
    op.drop_index("ix_alphas_run_id", table_name="alphas")
    op.drop_index("ix_alphas_expression_hash", table_name="alphas")
    op.drop_index("ix_research_candidates_task_signature", table_name="research_candidates")
