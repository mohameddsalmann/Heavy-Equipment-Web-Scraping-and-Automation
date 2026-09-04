"""Add alert_events table.

Revision ID: 002_alert_events
Revises: 001_initial_schema
Create Date: 2026-09-03 04:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "002_alert_events"
down_revision: Union[str, None] = "001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "alert_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("listing_id", sa.Uuid(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["listing_id"], ["listings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_alert_events_event_type", "alert_events", ["event_type"])
    op.create_index("ix_alert_events_listing_id", "alert_events", ["listing_id"])
    op.create_index("ix_alert_events_created_at", "alert_events", ["created_at"])
    op.create_index("ix_alert_events_delivered_at", "alert_events", ["delivered_at"])


def downgrade() -> None:
    op.drop_index("ix_alert_events_delivered_at", table_name="alert_events")
    op.drop_index("ix_alert_events_created_at", table_name="alert_events")
    op.drop_index("ix_alert_events_listing_id", table_name="alert_events")
    op.drop_index("ix_alert_events_event_type", table_name="alert_events")
    op.drop_table("alert_events")
