"""Align database schema with features.html requirements.

Revision ID: 202606240002
Revises: 202606230001
Create Date: 2026-06-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "202606240002"
down_revision: Union[str, None] = "202606230001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─── Table: users ───
    op.add_column("users", sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("users", sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()))

    # ─── Table: text_to_speech ───
    # Rename columns
    op.alter_column("text_to_speech", "detail", new_column_name="input_text")
    op.alter_column("text_to_speech", "audio", new_column_name="audio_url")
    op.alter_column("text_to_speech", "current_time", new_column_name="created_at")
    op.alter_column("text_to_speech", "updating_time", new_column_name="completed_at")
    
    # Add new columns
    op.add_column("text_to_speech", sa.Column("language", sa.String(length=10), nullable=True))
    op.add_column("text_to_speech", sa.Column("model_used", sa.String(length=50), nullable=True))
    op.add_column("text_to_speech", sa.Column("audio_bytes", sa.LargeBinary(), nullable=True))

    # ─── Table: speech_to_text ───
    # Rename columns
    op.alter_column("speech_to_text", "audio", new_column_name="audio_url")
    op.alter_column("speech_to_text", "detail", new_column_name="transcript")
    op.alter_column("speech_to_text", "current_time", new_column_name="created_at")
    op.alter_column("speech_to_text", "updating_time", new_column_name="completed_at")
    op.alter_column("speech_to_text", "language", new_column_name="language_hint")
    
    # Add new columns
    op.add_column("speech_to_text", sa.Column("detected_language", sa.String(length=10), nullable=True))
    op.add_column("speech_to_text", sa.Column("audio_bytes", sa.LargeBinary(), nullable=True))
    op.add_column("speech_to_text", sa.Column("input_format", sa.String(length=20), nullable=True))
    op.add_column("speech_to_text", sa.Column("segments", sa.JSON(), nullable=True))


def downgrade() -> None:
    # ─── Table: speech_to_text ───
    op.drop_column("speech_to_text", "segments")
    op.drop_column("speech_to_text", "input_format")
    op.drop_column("speech_to_text", "audio_bytes")
    op.drop_column("speech_to_text", "detected_language")
    
    op.alter_column("speech_to_text", "language_hint", new_column_name="language")
    op.alter_column("speech_to_text", "completed_at", new_column_name="updating_time")
    op.alter_column("speech_to_text", "created_at", new_column_name="current_time")
    op.alter_column("speech_to_text", "transcript", new_column_name="detail")
    op.alter_column("speech_to_text", "audio_url", new_column_name="audio")

    # ─── Table: text_to_speech ───
    op.drop_column("text_to_speech", "audio_bytes")
    op.drop_column("text_to_speech", "model_used")
    op.drop_column("text_to_speech", "language")
    
    op.alter_column("text_to_speech", "completed_at", new_column_name="updating_time")
    op.alter_column("text_to_speech", "created_at", new_column_name="current_time")
    op.alter_column("text_to_speech", "audio_url", new_column_name="audio")
    op.alter_column("text_to_speech", "input_text", new_column_name="detail")

    # ─── Table: users ───
    op.drop_column("users", "created_at")
    op.drop_column("users", "is_verified")
