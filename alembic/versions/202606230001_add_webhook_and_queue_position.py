"""Add webhook_url, webhook_sent_at, and queue_position columns.

Revision ID: 202606230001
Revises: 202606220001
Create Date: 2026-06-23
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "202606230001"
down_revision = "202606220001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # TextToSpeech table
    op.add_column("text_to_speech", sa.Column("queue_position", sa.Integer(), nullable=True))
    op.add_column("text_to_speech", sa.Column("webhook_url", sa.String(2048), nullable=True))
    op.add_column("text_to_speech", sa.Column("webhook_sent_at", sa.DateTime(), nullable=True))

    # SpeechToText table
    op.add_column("speech_to_text", sa.Column("queue_position", sa.Integer(), nullable=True))
    op.add_column("speech_to_text", sa.Column("webhook_url", sa.String(2048), nullable=True))
    op.add_column("speech_to_text", sa.Column("webhook_sent_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("speech_to_text", "webhook_sent_at")
    op.drop_column("speech_to_text", "webhook_url")
    op.drop_column("speech_to_text", "queue_position")

    op.drop_column("text_to_speech", "webhook_sent_at")
    op.drop_column("text_to_speech", "webhook_url")
    op.drop_column("text_to_speech", "queue_position")
