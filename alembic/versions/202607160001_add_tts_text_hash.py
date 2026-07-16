"""Add text_hash column to text_to_speech for content-based audio caching.

Lets the gateway short-circuit identical (text, voice, format) requests to a
previously synthesized clip instead of re-queuing a job and re-running the model.

Revision ID: 202607160001
Revises: 202607020001
Create Date: 2026-07-16
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "202607160001"
down_revision = "202607020001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("text_to_speech", sa.Column("text_hash", sa.String(64), nullable=True))
    op.create_index(
        "ix_text_to_speech_text_hash", "text_to_speech", ["text_hash"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_text_to_speech_text_hash", table_name="text_to_speech")
    op.drop_column("text_to_speech", "text_hash")
