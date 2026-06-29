-- ============================================================
-- Voice Gateway API — Full PostgreSQL Database Setup
-- ============================================================
--
-- ARCHITECTURE:
--   Only the Voice Gateway service connects to this database.
--   The TTS and STT microservices are HTTP-only — they do NOT
--   access the database. The gateway proxies requests to them
--   and stores results here.
--
--       Client
--         │
--         ▼
--   ┌─────────────────┐       ┌──────────────┐
--   │  Voice Gateway   │──────▶│  PostgreSQL   │
--   │  (FastAPI :8001) │       │  (this DB)    │
--   └────────┬─────────┘       └──────────────┘
--            │
--      ┌─────┴──────┐
--      ▼            ▼
--   TTS Service   STT Service
--   (HTTP :8000)  (HTTP :800x)
--   No DB access  No DB access
--
-- ============================================================
--
-- STEP 1 — Create the database and role (run as superuser):
--
--   CREATE ROLE voicegw WITH LOGIN PASSWORD 'your-strong-password';
--   CREATE DATABASE voice_gateway OWNER voicegw;
--   GRANT ALL PRIVILEGES ON DATABASE voice_gateway TO voicegw;
--
-- STEP 2 — Apply this schema:
--
--   psql -h <host> -U voicegw -d voice_gateway -f schema.sql
--
-- STEP 3 — Set the gateway's .env to connect:
--
--   DATABASE_URL=postgresql+psycopg2://voicegw:<password>@<host>:5432/voice_gateway
--   ENVIRONMENT=production
--   CREATE_DB_TABLES=false
--   JWT_SECRET=<your-long-random-secret>
--
-- STEP 4 — Configure microservice URLs in the gateway's .env:
--
--   TTS_ENGINE_URL=http://<tts-host>:8000
--   TTS_ENGINE_PATH=/v1/tts
--   STT_ENGINE_URL=http://<stt-host>:8000
--   STT_ENGINE_PATH=/v1/stt
--
-- ============================================================

BEGIN;

-- ----------------------------------------------------------
-- 1. users
-- ----------------------------------------------------------
-- Stores user accounts, hashed passwords, API keys, and
-- usage counters. The gateway creates rows on /signup.
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    user_id          SERIAL        PRIMARY KEY,
    password         VARCHAR(255)  NOT NULL,
    email            VARCHAR(255)  NOT NULL UNIQUE,
    api_key          VARCHAR(64)   NOT NULL UNIQUE,
    login_time       TIMESTAMP     NULL,
    signout_time     TIMESTAMP     NULL,
    total_processing INTEGER       NOT NULL DEFAULT 0,
    total_failed     INTEGER       NOT NULL DEFAULT 0,
    is_verified      BOOLEAN       NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMP     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_users_user_id  ON users (user_id);
CREATE INDEX IF NOT EXISTS ix_users_email    ON users (email);
CREATE INDEX IF NOT EXISTS ix_users_api_key  ON users (api_key);

-- ----------------------------------------------------------
-- 2. text_to_speech
-- ----------------------------------------------------------
-- One row per TTS job. The gateway creates a row, forwards
-- the request to the TTS microservice over HTTP, saves the
-- returned audio file, then updates this row with the
-- audio path and processing time.
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS text_to_speech (
    request_id      SERIAL            PRIMARY KEY,
    audio           VARCHAR(512)      NULL,
    detail          TEXT              NOT NULL,
    user_id         INTEGER           NOT NULL
                        REFERENCES users (user_id)
                        ON DELETE CASCADE,
    current_time    TIMESTAMP         NOT NULL DEFAULT NOW(),
    updating_time   TIMESTAMP         NULL,
    processing_time DOUBLE PRECISION  NULL
);

CREATE INDEX IF NOT EXISTS ix_text_to_speech_request_id ON text_to_speech (request_id);
CREATE INDEX IF NOT EXISTS idx_tts_user                 ON text_to_speech (user_id);

-- ----------------------------------------------------------
-- 3. speech_to_text
-- ----------------------------------------------------------
-- One row per STT job. The gateway saves the uploaded audio,
-- creates a row, forwards the file to the STT microservice
-- over HTTP, then updates this row with the transcript and
-- processing time.
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS speech_to_text (
    request_id      SERIAL            PRIMARY KEY,
    audio           VARCHAR(512)      NOT NULL,
    detail          TEXT              NULL,
    user_id         INTEGER           NOT NULL
                        REFERENCES users (user_id)
                        ON DELETE CASCADE,
    current_time    TIMESTAMP         NOT NULL DEFAULT NOW(),
    updating_time   TIMESTAMP         NULL,
    processing_time DOUBLE PRECISION  NULL
);

CREATE INDEX IF NOT EXISTS ix_speech_to_text_request_id ON speech_to_text (request_id);
CREATE INDEX IF NOT EXISTS idx_stt_user                 ON speech_to_text (user_id);
-- ----------------------------------------------------------
-- 4. otp_verifications
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS otp_verifications (
    id          SERIAL        PRIMARY KEY,
    user_id     INTEGER       NOT NULL
                    REFERENCES users (user_id)
                    ON DELETE CASCADE,
    otp_code    VARCHAR(6)    NOT NULL,
    purpose     VARCHAR(20)   NOT NULL,
    is_used     BOOLEAN       NOT NULL DEFAULT FALSE,
    expires_at  TIMESTAMP     NOT NULL,
    created_at  TIMESTAMP     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_otp_user ON otp_verifications (user_id);

-- ----------------------------------------------------------
-- 5. rate_limits
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS rate_limits (
    id             SERIAL    PRIMARY KEY,
    user_id        INTEGER   NOT NULL
                       REFERENCES users (user_id)
                       ON DELETE CASCADE,
    endpoint       VARCHAR(50)  NOT NULL,
    window_minute  VARCHAR(20)  NOT NULL,
    window_day     VARCHAR(10)  NOT NULL,
    rpm_count      INTEGER      NOT NULL DEFAULT 0,
    rpd_count      INTEGER      NOT NULL DEFAULT 0,
    created_at     TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMP    NULL
);

CREATE INDEX IF NOT EXISTS ix_rate_limits_user ON rate_limits (user_id);

-- ----------------------------------------------------------
-- 6. error_logs
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS error_logs (
    id            SERIAL        PRIMARY KEY,
    user_id       INTEGER       NULL
                      REFERENCES users (user_id)
                      ON DELETE SET NULL,
    endpoint      VARCHAR(255)  NOT NULL,
    method        VARCHAR(10)   NOT NULL,
    error_type    VARCHAR(100)  NOT NULL,
    status_code   INTEGER       NULL,
    error_message TEXT          NOT NULL,
    created_at    TIMESTAMP     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_error_logs_user ON error_logs (user_id);

-- ----------------------------------------------------------
-- 7. conversations
-- ----------------------------------------------------------
-- One row per chat/translation conversation. Persists the
-- chatbot's history server-side (instead of browser-only),
-- scoped to the owning user.
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id  SERIAL        PRIMARY KEY,
    user_id          INTEGER       NOT NULL
                         REFERENCES users (user_id)
                         ON DELETE CASCADE,
    title            VARCHAR(255)  NOT NULL DEFAULT 'New Chat',
    mode             VARCHAR(20)   NOT NULL DEFAULT 'chat',  -- 'chat' | 'translate'
    created_at       TIMESTAMP     NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMP     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_conversations_conversation_id ON conversations (conversation_id);
CREATE INDEX IF NOT EXISTS ix_conversations_user_id         ON conversations (user_id);

-- ----------------------------------------------------------
-- 8. chat_messages
-- ----------------------------------------------------------
-- One row per message in a conversation. Translation turns
-- additionally store source/target language and the engine
-- used ('llm' or 'api').
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS chat_messages (
    message_id       SERIAL        PRIMARY KEY,
    conversation_id  INTEGER       NOT NULL
                         REFERENCES conversations (conversation_id)
                         ON DELETE CASCADE,
    user_id          INTEGER       NOT NULL
                         REFERENCES users (user_id)
                         ON DELETE CASCADE,
    role             VARCHAR(20)   NOT NULL,   -- 'user' | 'assistant' | 'system'
    content          TEXT          NOT NULL,
    source_lang      VARCHAR(10)   NULL,
    target_lang      VARCHAR(10)   NULL,
    engine           VARCHAR(20)   NULL,
    created_at       TIMESTAMP     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_chat_messages_message_id      ON chat_messages (message_id);
CREATE INDEX IF NOT EXISTS ix_chat_messages_conversation_id ON chat_messages (conversation_id);
CREATE INDEX IF NOT EXISTS ix_chat_messages_user_id         ON chat_messages (user_id);
CREATE INDEX IF NOT EXISTS ix_chat_messages_conv_msg        ON chat_messages (conversation_id, message_id);

COMMIT;
