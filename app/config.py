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

    # Audio storage
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
