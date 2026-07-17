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
    allowed_origins: list[str] = [
        "http://localhost:3000",
        "http://localhost:8001",
        "http://localhost:5173",
        "https://main.dexaitech.com",
    ]
    create_db_tables: bool = True

    # Database
    database_url: str = "sqlite:///./voice_gateway.db"
    db_pool_size: int = 10          # persistent connections per worker
    db_max_overflow: int = 20       # extra burst connections
    db_pool_recycle: int = 1800     # recycle connections after N seconds

    # JWT
    jwt_secret: str = "dev-secret-change-me-in-production"
    jwt_expires: int = Field(default=3600, description="JWT token expiry in seconds")

    # LLM service microservice — the chatbot backend (chat, translate, voice TTS/STT).
    # The gateway reverse-proxies /api/* and /v1/* to this service, adding API-key
    # management on top. Point this at wherever the LLM service is deployed.
    llm_service_url: str = "http://185.14.252.20:8008"
    llm_service_timeout: float = 120.0
    # Require a gateway X-API-Key on the proxied LLM routes.
    # True = one gateway API key unlocks ALL features (chat, translate, voice,
    # history) for external clients. The internal hops stay keyless: gateway →
    # LLM service → vLLM (llama, VLLM_API_KEY=EMPTY). Set False only for open demos.
    llm_require_api_key: bool = True
    # Shared secret the gateway sends to the LLM service (X-Service-Key) so the
    # LLM service rejects direct-IP hits that bypass the gateway. Must match the
    # LLM service's SERVICE_API_KEY. Empty = don't send one.
    llm_service_api_key: str = ""
    # Per-user rate limiting on the proxied LLM routes (protects the GPU from abuse).
    llm_rate_limit_enabled: bool = False
    rate_limit_rpm: int = 50
    rate_limit_rpd: int = 1000


    # Voice engines
    tts_engine_url: str = "http://localhost:8000"
    tts_engine_path: str = "/v1/tts"
    tts_allowed_formats: list[str] = ["wav", "mp3"]
    max_tts_text_chars: int = 500
    stt_engine_url: str | None = None
    stt_engine_path: str = "/v1/stt"
    engine_timeout_seconds: float = 60.0

    # Audio storage — local fallback
    audio_storage_dir: str = "audio_storage"
    max_audio_upload_bytes: int = 25 * 1024 * 1024
    # Keep this aligned with the STT engine's MIME_TO_FORMAT
    # (kdext_conversa_ai_stt/app/utils/webm_to_wav.py) — the content_type is
    # forwarded to the engine verbatim, so a type allowed here must be accepted
    # there too.
    allowed_audio_content_types: list[str] = [
        "audio/wav",
        "audio/wave",
        "audio/x-wav",
        "audio/mpeg",
        "audio/mp3",
        "audio/mp4",
        "audio/m4a",
        "audio/x-m4a",
        "audio/webm",
        "audio/ogg",
        "audio/flac",
    ]
    # Email / OTP
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = "noreply@kdext.ai"
    otp_expires_minutes: int = 10
    # AWS S3 — set USE_S3_STORAGE=true to enable cloud audio storage
    use_s3_storage: bool = False
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_s3_bucket: str | None = None
    aws_s3_region: str = "us-east-1"
    # Generated audio is served via a short-lived, unguessable URL minted only
    # for the authenticated job owner (presigned S3 URL in S3 mode, HMAC-signed
    # /audio path in local mode). The browser fetches it immediately after the
    # job completes, so a short TTL is enough and limits the bearer-URL window.
    # 600s leaves margin for long clips or pause/seek after load (a range
    # request past expiry would 403).
    audio_url_ttl_seconds: int = 600

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

    @field_validator("jwt_expires", mode="before")
    @classmethod
    def empty_expires_is_default(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return 3600
        if value is None:
            return 3600
        return value

    @field_validator("smtp_port", mode="before")
    @classmethod
    def empty_port_is_default(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return 587
        if value is None:
            return 587
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
