"""Add async job queue columns to TTS and STT tables.

Revision ID: 202606220001
Revises: 202606130001
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "202606220001"
down_revision = "202606130001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # TextToSpeech table
    op.add_column("text_to_speech", sa.Column("status", sa.String(20), nullable=False, server_default="completed"))
    op.add_column("text_to_speech", sa.Column("error_message", sa.Text(), nullable=True))
    op.add_column("text_to_speech", sa.Column("voice", sa.String(256), nullable=True))
    op.add_column("text_to_speech", sa.Column("format", sa.String(16), nullable=True, server_default="wav"))
    op.create_index("ix_text_to_speech_status", "text_to_speech", ["status"])

    # SpeechToText table
    op.add_column("speech_to_text", sa.Column("status", sa.String(20), nullable=False, server_default="completed"))
    op.add_column("speech_to_text", sa.Column("error_message", sa.Text(), nullable=True))
    op.add_column("speech_to_text", sa.Column("language", sa.String(10), nullable=True))
    op.create_index("ix_speech_to_text_status", "speech_to_text", ["status"])


def downgrade() -> None:
    op.drop_index("ix_speech_to_text_status", table_name="speech_to_text")
    op.drop_column("speech_to_text", "language")
    op.drop_column("speech_to_text", "error_message")
    op.drop_column("speech_to_text", "status")

    op.drop_index("ix_text_to_speech_status", table_name="text_to_speech")
    op.drop_column("text_to_speech", "format")
    op.drop_column("text_to_speech", "voice")
    op.drop_column("text_to_speech", "error_message")
    op.drop_column("text_to_speech", "status")
