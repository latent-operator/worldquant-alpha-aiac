"""Persist quota wait and evolution checkpoint on experiment runs."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_llm_wait_checkpoint"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade():
    # The initial migration uses metadata.create_all, so fresh installs may
    # already have these columns before this incremental revision runs.
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("experiment_runs")}
    if "checkpoint" not in existing:
        op.add_column("experiment_runs", sa.Column("checkpoint", JSONB(), nullable=True))
    if "block_reason" not in existing:
        op.add_column("experiment_runs", sa.Column("block_reason", sa.String(100), nullable=True))
    if "next_retry_at" not in existing:
        op.add_column("experiment_runs", sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("experiment_runs", "next_retry_at")
    op.drop_column("experiment_runs", "block_reason")
    op.drop_column("experiment_runs", "checkpoint")
