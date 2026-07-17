# Voice Gateway API

FastAPI gateway for authentication, request validation, voice-engine routing, usage accounting, and audit logging in front of Text-to-Speech and Speech-to-Text services.

Clients should call this gateway, not the TTS/STT microservices directly. The gateway owns user accounts, API keys, JWT login, database records, counters, and audio file URLs.

## Table of Contents

- [Architecture](#architecture)
- [How Requests Move Through The System](#how-requests-move-through-the-system)
- [Project Structure](#project-structure)
- [Endpoints](#endpoints)
- [Authentication](#authentication)
- [Voice Engine Integration](#voice-engine-integration)
- [Database](#database)
- [Environment Variables](#environment-variables)
- [Local Development](#local-development)
- [Production Setup](#production-setup)
- [Docker](#docker)
- [Testing](#testing)
- [Common Developer Tasks](#common-developer-tasks)
- [Production Checklist](#production-checklist)

## Architecture

```mermaid
flowchart LR
    Client["Client / Mobile App / Backend"] -->|"HTTPS"| Gateway["Voice Gateway API<br/>FastAPI on :8001"]

    subgraph GatewayBox["Gateway Responsibilities"]
        Auth["Auth<br/>JWT + X-API-Key"]
        Validation["Pydantic Validation"]
        Accounting["Usage Accounting"]
        Audit["TTS/STT Job Audit Rows"]
        Storage["Audio Storage<br/>local folder now, S3-ready later"]
    end

    Gateway --> Auth
    Gateway --> Validation
    Gateway --> Accounting
    Gateway --> Audit
    Gateway --> Storage

    Gateway -->|"SQLAlchemy"| DB[("PostgreSQL<br/>production")]
    Gateway -->|"POST /v1/tts JSON"| TTS["TTS Microservice<br/>Desktop/tts on :8000"]
    Gateway -->|"multipart /v1/stt"| STT["STT Microservice<br/>optional"]
```

The current Desktop TTS service is in:

```text
/Users/taqaddusshafi/Desktop/tts
```

It exposes:

```text
POST http://localhost:8000/v1/tts
```

and returns `audio/wav`.

## How Requests Move Through The System

### Signup And Login

```mermaid
sequenceDiagram
    participant C as Client
    participant G as Gateway
    participant D as Database

    C->>G: POST /signup email + password
    G->>G: Hash password and generate API key
    G->>D: Insert user
    G-->>C: user_id, email, api_key

    C->>G: POST /login email + password
    G->>D: Find user
    G->>G: Verify password and create JWT
    G->>D: Update login_time
    G-->>C: access_token, token_type, api_key, expires_in
```

### Text To Speech

```mermaid
sequenceDiagram
    participant C as Client
    participant G as Gateway
    participant D as Database
    participant T as TTS Service
    participant S as Audio Storage

    C->>G: POST /text-to-speech with X-API-Key
    G->>D: Validate API key
    G->>D: Create text_to_speech job
    G->>T: POST /v1/tts {text, language, voice}
    T-->>G: audio/wav bytes
    G->>S: Save output audio
    G->>D: Update job, processing_time, total_processing
    G-->>C: request_id, audio_url, detail, processing_time
```

### Speech To Text

```mermaid
sequenceDiagram
    participant C as Client
    participant G as Gateway
    participant D as Database
    participant E as STT Service
    participant S as Audio Storage

    C->>G: POST /speech-to-text multipart file + optional language
    G->>D: Validate API key
    G->>G: Validate content type and size
    G->>S: Save uploaded audio
    G->>D: Create/update speech_to_text job
    alt STT_ENGINE_URL configured
        G->>E: Forward multipart file
        E-->>G: transcript
    else Local development
        G->>G: Use stub transcript
    end
    G->>D: Update job, processing_time, total_processing
    G-->>C: request_id, detail, audio_url, processing_time
```

## Project Structure

```text
voice-gateway/
├── app/
│   ├── main.py                  # FastAPI app, middleware, routes, health/readiness
│   ├── config.py                # Environment-driven settings
│   ├── database.py              # SQLAlchemy engine/session
│   ├── core/
│   │   ├── dependencies.py      # JWT and API-key dependencies
│   │   ├── logging.py           # Logging setup
│   │   └── security.py          # Password hashing, JWT, API-key generation
│   ├── models/
│   │   ├── user.py              # users table
│   │   ├── tts.py               # text_to_speech table
│   │   └── stt.py               # speech_to_text table
│   ├── routers/
│   │   ├── auth.py              # /signup, /login
│   │   ├── profile.py           # /profile
│   │   ├── tts.py               # /text-to-speech
│   │   └── stt.py               # /speech-to-text
│   ├── schemas/
│   │   ├── auth.py              # Signup/Login schemas
│   │   ├── user.py              # Profile response schema
│   │   ├── tts.py               # TTS request/response schema
│   │   └── stt.py               # STT response schema
│   ├── services/
│   │   ├── tts_service.py       # Calls TTS microservice
│   │   ├── stt_service.py       # Calls STT microservice or stub
│   │   └── usage.py             # Counter helpers
│   └── storage/
│       └── audio_store.py       # Local audio file storage
├── alembic/
│   ├── env.py
│   └── versions/                # Database migrations
├── tests/
├── .env.example
├── alembic.ini
├── Dockerfile
├── requirements.txt
└── README.md
```

## Endpoints

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/health` | none | Process health check |
| `GET` | `/ready` | none | Database readiness check |
| `POST` | `/signup` | none | Create account and issue API key |
| `POST` | `/verify-otp` | none | Verify email with OTP after signup |
| `POST` | `/login` | none | Authenticate and issue JWT |
| `GET` | `/profile` | JWT bearer token | Return user profile and usage stats |
| `POST` | `/text-to-speech` | `X-API-Key` | Proxy TTS request and save generated audio |
| `POST` | `/speech-to-text` | `X-API-Key` | Upload audio and transcribe through STT service or stub |


### Example: Signup

```bash
curl -X POST http://localhost:8001/signup \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com","password":"StrongPass123"}'
```

### Example: Login

```bash
curl -X POST http://localhost:8001/login \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com","password":"StrongPass123"}'
```

### Example: Profile

```bash
curl http://localhost:8001/profile \
  -H "Authorization: Bearer <access_token>"
```

### Example: Text To Speech

```bash
curl -X POST http://localhost:8001/text-to-speech \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <api_key>" \
  -d '{"text":"Hello, welcome to the voice gateway.","voice":"en-US-female-1","format":"wav"}'
```

The gateway calls the TTS service with only:

```json
{
  "text": "Hello, welcome to the voice gateway.",
  "language": "en",
  "voice": "en-US-female-1"
}
```

That matches the current `tts` microservice schema, which forbids extra fields.

### Example: Speech To Text

```bash
curl -X POST http://localhost:8001/speech-to-text \
  -H "X-API-Key: <api_key>" \
  -F "file=@sample.wav;type=audio/wav" \
  -F "language=en"
```

## Authentication

The project uses two auth mechanisms:

| Mechanism | Used By | Header |
| --- | --- | --- |
| JWT access token | Account/profile endpoints | `Authorization: Bearer <token>` |
| API key | Voice-processing endpoints | `X-API-Key: <api_key>` |

Why both:

- JWTs are short-lived and good for user account flows.
- API keys are long-lived and good for server-to-server voice processing calls.

## Voice Engine Integration

### TTS

Default gateway config:

```env
TTS_ENGINE_URL=http://localhost:8000
TTS_ENGINE_PATH=/v1/tts
TTS_ALLOWED_FORMATS=wav,mp3
MAX_TTS_TEXT_CHARS=500
```

The Desktop TTS service runs from:

```bash
cd /Users/taqaddusshafi/Desktop/tts
python run.py
```

The gateway runs from:

```bash
cd /Users/taqaddusshafi/Desktop/voice-gateway
python run.py
```

Call the gateway on port `8001`; it forwards valid TTS jobs to the TTS service on port `8000`.

### STT

Default development mode:

```env
STT_ENGINE_URL=
```

When `STT_ENGINE_URL` is empty, `/speech-to-text` uses the current stub transcript so the API remains testable.

Production mode:

```env
STT_ENGINE_URL=http://stt-engine:8000
STT_ENGINE_PATH=/v1/stt
```

The gateway forwards multipart form data with:

- `file`
- optional `language`

The STT engine response can be JSON with one of these transcript keys:

- `detail`
- `text`
- `transcript`
- `transcription`

or plain text.

## Database

Production uses PostgreSQL. Local development can use SQLite.

```mermaid
erDiagram
    users ||--o{ text_to_speech : owns
    users ||--o{ speech_to_text : owns
    users ||--o{ otp_verifications : owns
    users ||--o{ rate_limits : owns
    users ||--o{ error_logs : owns

    users {
        integer user_id PK
        varchar password
        varchar email UK
        varchar api_key UK
        timestamp login_time
        timestamp signout_time
        integer total_processing
        integer total_failed
        boolean is_verified
        timestamp created_at
    }

    text_to_speech {
        integer request_id PK
        varchar audio
        text detail
        integer user_id FK
        timestamp current_time
        timestamp updating_time
        float processing_time
    }

    speech_to_text {
        integer request_id PK
        varchar audio
        text detail
        integer user_id FK
        timestamp current_time
        timestamp updating_time
        float processing_time
    }
    otp_verifications {
        integer id PK
        integer user_id FK
        varchar otp_code
        varchar purpose
        boolean is_used
        timestamp expires_at
        timestamp created_at
    }

    rate_limits {
        integer id PK
        integer user_id FK
        varchar endpoint
        varchar window_minute
        varchar window_day
        integer rpm_count
        integer rpd_count
    }

    error_logs {
        integer id PK
        integer user_id FK
        varchar endpoint
        varchar method
        varchar error_type
        integer status_code
        text error_message
        timestamp created_at
    }
```

Tables:

- `users`: account identity, hashed password, API key, counters.
- `text_to_speech`: one row per TTS job.
- `speech_to_text`: one row per STT job.

Counters:

- `total_processing`: increments on successful TTS/STT jobs.
- `total_failed`: increments on failed engine calls or processing errors.

## Environment Variables

| Variable | Local Example | Production Guidance |
| --- | --- | --- |
| `ENVIRONMENT` | `local` | Use `production` in prod |
| `DATABASE_URL` | `sqlite:///./voice_gateway.db` | Use `postgresql+psycopg2://...` |
| `JWT_SECRET` | dev value | Use a long random secret |
| `JWT_EXPIRES` | `3600` | Tune per security policy |
| `CREATE_DB_TABLES` | `true` | Must be `false` in production |
| `ALLOWED_ORIGINS` | `http://localhost:3000,http://localhost:8001` | Use real frontend domains |
| `TTS_ENGINE_URL` | `http://localhost:8000` | Internal TTS service URL |
| `TTS_ENGINE_PATH` | `/v1/tts` | Keep aligned with TTS service |
| `TTS_ALLOWED_FORMATS` | `wav,mp3` | Output formats the TTS engine can return |
| `MAX_TTS_TEXT_CHARS` | `500` | Match TTS service limit |
| `STT_ENGINE_URL` | empty | Set real STT service URL |
| `STT_ENGINE_PATH` | `/v1/stt` | Keep aligned with STT service |
| `AUDIO_STORAGE_DIR` | `audio_storage` | Use durable disk or S3 replacement |
| `MAX_AUDIO_UPLOAD_BYTES` | `5242880` | 5 MB upload limit |
| `SMTP_HOST` | `smtp.gmail.com` | Gmail SMTP host |
| `SMTP_PORT` | `587` | SMTP port |
| `SMTP_USER` | empty | Gmail address for sending OTP emails |
| `SMTP_PASSWORD` | empty | Gmail app password |
| `EMAIL_FROM` | `noreply@kdext.ai` | Sender address in OTP emails |
| `OTP_EXPIRES_MINUTES` | `10` | OTP validity window in minutes |

Copy the template:

```bash
cp .env.example .env
```

## Local Development

Install and run:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload --host 0.0.0.0 --port 8001
```

Open docs:

```text
http://localhost:8001/docs
```

Start the TTS microservice separately:

```bash
cd /Users/taqaddusshafi/Desktop/tts
python run.py
```

Then call gateway endpoints on:

```text
http://localhost:8001
```

## Production Setup

Use PostgreSQL and Alembic migrations. Do not rely on automatic table creation.

Example production `.env`:

```env
ENVIRONMENT=production
DATABASE_URL=postgresql+psycopg2://voicegw:strong-password@db-host:5432/voice_gateway
JWT_SECRET=replace-with-a-long-random-secret
JWT_EXPIRES=3600
CREATE_DB_TABLES=false
ALLOWED_ORIGINS=https://your-frontend.example.com
TTS_ENGINE_URL=http://tts-engine:8000
TTS_ENGINE_PATH=/v1/tts
STT_ENGINE_URL=http://stt-engine:8000
STT_ENGINE_PATH=/v1/stt
AUDIO_STORAGE_DIR=/var/lib/voice-gateway/audio_storage
```

Deploy:

```bash
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8001
```

Recommended production shape:

- Run gateway behind HTTPS/load balancer/reverse proxy.
- Run PostgreSQL as managed DB or durable container.
- Run TTS and STT services on private network URLs.
- Mount `AUDIO_STORAGE_DIR` on durable storage or replace local storage with S3.
- Keep `.env` secrets out of git.
- Set `CREATE_DB_TABLES=false`.
- Run `alembic upgrade head` during deploy/release.

## Docker

Build:

```bash
docker build -t voice-gateway .
```

Run:

```bash
docker run --env-file .env -p 8001:8001 voice-gateway
```

Run migrations as a release step:

```bash
docker run --env-file .env voice-gateway alembic upgrade head
```

## Testing

Run the full test suite:

```bash
.venv/bin/python -m pytest tests/ -v
```

Run the smoke verification script:

```bash
.venv/bin/python verify.py
```

Current coverage includes:

- health endpoint
- signup/login/profile
- invalid email validation
- TTS proxy behavior
- TTS payload compatibility with the Desktop `tts` microservice
- unsupported TTS format rejection
- STT language forwarding
- unsupported STT media type rejection
- audio filename sanitization
- path traversal rejection
- OTP verification flow (signup → verify-otp → login)
- rate limiting (RPM and RPD per user per endpoint)
- error logging to database

## Common Developer Tasks

### Add A New Endpoint

1. Add request/response schemas in `app/schemas/`.
2. Add route handler in `app/routers/`.
3. Add service logic in `app/services/` if the route needs business logic.
4. Register the router in `app/main.py`.
5. Add tests in `tests/test_gateway.py`.

### Add A Database Column

1. Update the SQLAlchemy model in `app/models/`.
2. Create a new Alembic migration.
3. Run `alembic upgrade head`.
4. Update schemas/tests if the API response changes.

### Change TTS Service Contract

1. Check `/Users/taqaddusshafi/Desktop/tts/app/api/schemas/tts.py`.
2. Update `app/services/tts_service.py`.
3. Update `tests/test_gateway.py::test_tts_service_payload_matches_tts_microservice`.
4. Update this README.

### Replace Local Audio Storage With S3

1. Keep `save_audio(relative_path, data) -> str` as the public function.
2. Replace internals of `app/storage/audio_store.py`.
3. Return a URL that clients can fetch.
4. Add tests for successful upload and unsafe path handling.

## Production Checklist

- PostgreSQL database created.
- `DATABASE_URL` points to PostgreSQL.
- `psycopg2-binary` installed from `requirements.txt`.
- `JWT_SECRET` is strong and not the dev value.
- `CREATE_DB_TABLES=false`.
- `alembic upgrade head` runs successfully.
- `ALLOWED_ORIGINS` contains only trusted frontend origins.
- TTS service is reachable from gateway at `TTS_ENGINE_URL + TTS_ENGINE_PATH`.
- STT service is reachable from gateway if production STT is required.
- `/health` returns `{"status":"ok"}`.
- `/ready` returns `{"status":"ready"}`.
- Tests pass in CI.
- Audio storage is durable.
- Logs are collected by the deployment platform.
