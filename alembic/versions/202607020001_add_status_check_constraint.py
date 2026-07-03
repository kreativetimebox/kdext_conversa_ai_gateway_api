"""Add CHECK constraint on status column for TTS and STT tables.

This prevents rows from ever being committed with an invalid status value,
catching logic bugs (e.g. NULL status, typos) at the DB layer instead of
silently writing bad data.

Revision ID: 202607020001
Revises: 202606290001
Create Date: 2026-07-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "202607020001"
down_revision: Union[str, None] = "202606290001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

VALID_STATUSES = ("queued", "processing", "completed", "failed")


def upgrade() -> None:
    # ── text_to_speech ──────────────────────────────────────────────────────
    # First coerce any legacy NULL or unexpected values to 'completed' so the
    # constraint addition does not fail on existing rows.
    op.execute(
        """
        UPDATE text_to_speech
        SET status = 'completed'
        WHERE status IS NULL OR status NOT IN ('queued', 'processing', 'completed', 'failed')
        """
    )
    op.create_check_constraint(
        "ck_tts_status_valid",
        "text_to_speech",
        "status IN ('queued', 'processing', 'completed', 'failed')",
    )

    # ── speech_to_text ──────────────────────────────────────────────────────
    op.execute(
        """
        UPDATE speech_to_text
        SET status = 'completed'
        WHERE status IS NULL OR status NOT IN ('queued', 'processing', 'completed', 'failed')
        """
    )
    op.create_check_constraint(
        "ck_stt_status_valid",
        "speech_to_text",
        "status IN ('queued', 'processing', 'completed', 'failed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_stt_status_valid", "speech_to_text", type_="check")
    op.drop_constraint("ck_tts_status_valid", "text_to_speech", type_="check")
