"""Environment-driven application configuration."""

from functools import lru_cache
from typing import Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables and .env files."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    environment: str = "local"
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8001
    allowed_origins: list[str] = ["http://localhost:3000", "http://localhost:8001"]
    create_db_tables: bool = True

    # Database
    database_url: str = "sqlite:///./voice_gateway.db"

    # JWT
    jwt_secret: str = "dev-secret-change-me-in-production"
    jwt_expires: int = Field(default=3600, description="JWT token expiry in seconds")

    # Voice engines
    tts_engine_url: str = "http://localhost:8000"
    tts_engine_path: str = "/v1/tts"
    tts_allowed_formats: list[str] = ["wav"]
    max_tts_text_chars: int = 1000
    stt_engine_url: str | None = None
    stt_engine_path: str = "/v1/stt"
    engine_timeout_seconds: float = 60.0

    # Audio storage — local fallback
    audio_storage_dir: str = "audio_storage"
    max_audio_upload_bytes: int = 25 * 1024 * 1024
    allowed_audio_content_types: list[str] = [
        "audio/wav",
        "audio/wave",
        "audio/x-wav",
        "audio/mpeg",
        "audio/mp3",
        "audio/mp4",
        "audio/x-m4a",
        "audio/webm",
        "audio/ogg",
    ]

    # AWS S3 — set USE_S3_STORAGE=true to enable cloud audio storage
    use_s3_storage: bool = False
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_s3_bucket: str | None = None
    aws_s3_region: str = "us-east-1"

    # Amazon SQS — async job queue (voice-worker microservice)
    use_async_queue: bool = False
    aws_sqs_region: str = "ap-southeast-2"
    aws_sqs_tts_queue_url: str | None = None
    aws_sqs_stt_queue_url: str | None = None

    @field_validator(
        "allowed_origins",
        "tts_allowed_formats",
        "allowed_audio_content_types",
        mode="before",
    )
    @classmethod
    def parse_csv_list(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("tts_engine_path", "stt_engine_path")
    @classmethod
    def paths_start_with_slash(cls, value: str) -> str:
        return value if value.startswith("/") else f"/{value}"

    @field_validator("stt_engine_url", mode="before")
    @classmethod
    def empty_stt_url_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "aws_access_key_id", "aws_secret_access_key", "aws_s3_bucket",
        "aws_sqs_tts_queue_url", "aws_sqs_stt_queue_url",
        mode="before",
    )
    @classmethod
    def empty_aws_str_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def validate_s3_settings(self) -> "Settings":
        if self.use_s3_storage:
            missing = [
                name for name, val in [
                    ("AWS_ACCESS_KEY_ID", self.aws_access_key_id),
                    ("AWS_SECRET_ACCESS_KEY", self.aws_secret_access_key),
                    ("AWS_S3_BUCKET", self.aws_s3_bucket),
                ]
                if not val
            ]
            if missing:
                raise ValueError(
                    f"USE_S3_STORAGE=true requires these env vars: {', '.join(missing)}"
                )
        return self

    @model_validator(mode="after")
    def validate_sqs_settings(self) -> "Settings":
        if self.use_async_queue:
            missing = [
                name for name, val in [
                    ("AWS_SQS_TTS_QUEUE_URL", self.aws_sqs_tts_queue_url),
                    ("AWS_SQS_STT_QUEUE_URL", self.aws_sqs_stt_queue_url),
                    ("AWS_ACCESS_KEY_ID", self.aws_access_key_id),
                    ("AWS_SECRET_ACCESS_KEY", self.aws_secret_access_key),
                ]
                if not val
            ]
            if missing:
                raise ValueError(
                    f"USE_ASYNC_QUEUE=true requires these env vars: {', '.join(missing)}"
                )
        return self

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        if self.environment.lower() in {"prod", "production"}:
            if self.jwt_secret == "dev-secret-change-me-in-production":
                raise ValueError("JWT_SECRET must be set to a strong secret in production")
            if self.database_url.startswith("sqlite"):
                raise ValueError("DATABASE_URL must use PostgreSQL in production")
            if self.create_db_tables:
                raise ValueError("CREATE_DB_TABLES must be false in production; run Alembic migrations")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return cached settings so all modules share the same configuration."""

    return Settings()
