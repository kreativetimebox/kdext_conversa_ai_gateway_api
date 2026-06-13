"""initial schema

Revision ID: 202606130001
Revises:
Create Date: 2026-06-13 14:10:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "202606130001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", sa.Integer(), primary_key=True),
        sa.Column("password", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("api_key", sa.String(length=64), nullable=False),
        sa.Column("login_time", sa.DateTime(), nullable=True),
        sa.Column("signout_time", sa.DateTime(), nullable=True),
        sa.Column("total_processing", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("api_key"),
    )
    op.create_index("ix_users_user_id", "users", ["user_id"])
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("ix_users_api_key", "users", ["api_key"])

    op.create_table(
        "text_to_speech",
        sa.Column("request_id", sa.Integer(), primary_key=True),
        sa.Column("audio", sa.String(length=512), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("current_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updating_time", sa.DateTime(), nullable=True),
        sa.Column("processing_time", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_text_to_speech_request_id", "text_to_speech", ["request_id"])
    op.create_index("idx_tts_user", "text_to_speech", ["user_id"])

    op.create_table(
        "speech_to_text",
        sa.Column("request_id", sa.Integer(), primary_key=True),
        sa.Column("audio", sa.String(length=512), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("current_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updating_time", sa.DateTime(), nullable=True),
        sa.Column("processing_time", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_speech_to_text_request_id", "speech_to_text", ["request_id"])
    op.create_index("idx_stt_user", "speech_to_text", ["user_id"])


def downgrade() -> None:
    op.drop_index("idx_stt_user", table_name="speech_to_text")
    op.drop_index("ix_speech_to_text_request_id", table_name="speech_to_text")
    op.drop_table("speech_to_text")

    op.drop_index("idx_tts_user", table_name="text_to_speech")
    op.drop_index("ix_text_to_speech_request_id", table_name="text_to_speech")
    op.drop_table("text_to_speech")

    op.drop_index("ix_users_api_key", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_index("ix_users_user_id", table_name="users")
    op.drop_table("users")
