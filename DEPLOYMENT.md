# Voice Gateway — Production Server Setup Guide

> This document covers deploying the **full Voice Gateway stack** on your own server.
> This is separate from the Vercel demo — the server version has authentication,
> a real database, S3 audio storage, and connects to your TTS + STT engines.

---

## Architecture Overview

```
                        ┌─────────────────────────────────┐
                        │         YOUR SERVER              │
                        │                                  │
  Frontend App  ───────▶│  Voice Gateway API               │
  (React/Next/etc)      │  (FastAPI, port 8001)            │
                        │         │         │              │
                        │         ▼         ▼              │
                        │   PostgreSQL   AWS S3            │
                        │   (RDS/local)  (audio files)     │
                        └─────┬───────────────────────────┘
                              │
                  ┌───────────┴────────────┐
                  ▼                        ▼
         TTS Engine                 STT Engine
         (port 8000)                (port 8002)
         e.g. 185.14.252.20         e.g. 185.14.252.20
```

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Ubuntu / Debian server | 20.04+ | Or any Linux distro |
| Python | 3.11+ | Use `python3 --version` to check |
| Docker + Docker Compose | Latest | Optional but recommended |
| PostgreSQL | 14+ | AWS RDS or self-hosted |
| AWS Account | — | For S3 audio storage |
| Domain / IP | — | For frontend to connect |

---

## Step 1 — Clone the Repository

```bash
git clone https://github.com/Taqaddusshafi/voice-gateway.git
cd voice-gateway
```

---

## Step 2 — Set Up PostgreSQL Database

### Option A — AWS RDS (Recommended for Production)

1. Go to [AWS RDS Console](https://console.aws.amazon.com/rds/) → **Create database**
2. Settings:
   - Engine: **PostgreSQL 16**
   - Template: **Free tier** (or Production for paid)
   - DB instance identifier: `voice-gateway`
   - Master username: `voicegw`
   - Master password: *(set a strong password)*
   - Initial database name: `voice_gateway`
3. Connectivity:
   - **Public access**: Yes (if your server is outside AWS VPC)
   - Security group: Allow **port 5432** from your server IP
4. After creation, note the **Endpoint**:
   ```
   voice-gateway.xxxx.ap-south-1.rds.amazonaws.com
   ```

### Option B — Install PostgreSQL on the Same Server

```bash
sudo apt update && sudo apt install -y postgresql postgresql-contrib
sudo -u postgres psql -c "CREATE USER voicegw WITH PASSWORD 'YourStrongPassword';"
sudo -u postgres psql -c "CREATE DATABASE voice_gateway OWNER voicegw;"
```

Connection string for local PostgreSQL:
```
postgresql+psycopg2://voicegw:YourStrongPassword@localhost:5432/voice_gateway
```

---

## Step 3 — Set Up AWS S3 Bucket (Audio Storage)

> Skip this step if you want to store audio locally (set `USE_S3_STORAGE=false`)

### Create S3 Bucket
1. [S3 Console](https://s3.console.aws.amazon.com/) → **Create bucket**
2. Name: `voice-gateway-audio` | Region: `ap-south-1` (or your region)
3. Uncheck **Block all public access** → Acknowledge
4. After creation → **Permissions** → **Bucket Policy**:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": "*",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::voice-gateway-audio/*"
  }]
}
```

### Create IAM User
1. [IAM Console](https://console.aws.amazon.com/iam/) → **Users** → **Create user**
2. Attach policy: **AmazonS3FullAccess**
3. **Security credentials** → **Create access key** → save both values

---

## Step 4 — Configure Environment Variables

```bash
cp .env.example .env
nano .env
```

Fill in every value:

```bash
# ── Application ──────────────────────────────────────────────────────────────
ENVIRONMENT=production
LOG_LEVEL=INFO
HOST=0.0.0.0
PORT=8001
CREATE_DB_TABLES=false            # Always false in production — use Alembic

# ── Database (PostgreSQL) ─────────────────────────────────────────────────────
DATABASE_URL=postgresql+psycopg2://voicegw:YOUR_PASSWORD@YOUR_RDS_ENDPOINT:5432/voice_gateway

# ── Security ──────────────────────────────────────────────────────────────────
# Generate: python3 -c "import secrets; print(secrets.token_hex(32))"
JWT_SECRET=your-very-long-random-secret-here
JWT_EXPIRES=3600

# ── CORS — your frontend domain(s) ───────────────────────────────────────────
ALLOWED_ORIGINS=https://yourfrontend.com,https://www.yourfrontend.com

# ── TTS Engine ────────────────────────────────────────────────────────────────
TTS_ENGINE_URL=http://185.14.252.20:8000
TTS_ENGINE_PATH=/v1/tts
TTS_ALLOWED_FORMATS=wav
MAX_TTS_TEXT_CHARS=500

# ── STT Engine ────────────────────────────────────────────────────────────────
STT_ENGINE_URL=http://185.14.252.20:8002
STT_ENGINE_PATH=/v1/stt
ENGINE_TIMEOUT_SECONDS=60

# ── Audio Storage ─────────────────────────────────────────────────────────────
MAX_AUDIO_UPLOAD_BYTES=5242880
ALLOWED_AUDIO_CONTENT_TYPES=audio/wav,audio/wave,audio/x-wav,audio/mpeg,audio/mp3,audio/mp4,audio/x-m4a,audio/webm,audio/ogg

USE_S3_STORAGE=true
AWS_ACCESS_KEY_ID=AKIAxxxxxxxxxxxxxxxx
AWS_SECRET_ACCESS_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
AWS_S3_BUCKET=voice-gateway-audio
AWS_S3_REGION=ap-south-1
```

---

## Step 5 — Run Database Migrations

```bash
pip install -r requirements.txt

# Creates all tables (users, text_to_speech, speech_to_text)
alembic upgrade head
```

Expected output:
```
INFO  [alembic.runtime.migration] Running upgrade  -> 202606130001, initial schema
```

---

## Step 6 — Deploy with Docker (Recommended)

```bash
docker compose up -d --build
docker compose logs -f

# Verify
curl http://localhost:8001/health
curl http://localhost:8001/ready
```

### Or Run Directly with Python

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

---

## Step 7 — Set Up Nginx Reverse Proxy

```bash
sudo apt install -y nginx
sudo nano /etc/nginx/sites-available/voice-gateway
```

```nginx
server {
    listen 80;
    server_name api.yourdomain.com;

    client_max_body_size 6M;

    location / {
        proxy_pass         http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/voice-gateway /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# Add HTTPS
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d api.yourdomain.com
```

---

## Step 8 — Connect Frontend

Your frontend calls `https://api.yourdomain.com`.

### Available Endpoints

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `POST` | `/signup` | None | Create account → returns `api_key` |
| `POST` | `/login` | None | Login → returns JWT + `api_key` |
| `POST` | `/text-to-speech` | `X-Api-Key` | Text → audio S3 URL |
| `POST` | `/speech-to-text` | `X-Api-Key` | Audio file → transcript |
| `GET` | `/voices` | None | List TTS voices |
| `GET` | `/health` | None | Liveness check |
| `GET` | `/ready` | None | DB connectivity check |

### Auth Flow

```
1. POST /signup → { "api_key": "vgw_abc123..." }
2. Store api_key in frontend
3. All TTS/STT calls include: headers: { "X-Api-Key": "vgw_abc123..." }
```

### TTS Example (JavaScript)

```javascript
const formData = new FormData();
formData.append('text', 'Hello World');
formData.append('voice', 'aria');
formData.append('language', 'en');

const res = await fetch('https://api.yourdomain.com/text-to-speech', {
  method: 'POST',
  headers: { 'X-Api-Key': userApiKey },
  body: formData,
});
const data = await res.json();
// data.audio_url = "https://your-bucket.s3.ap-south-1.amazonaws.com/tts/1.wav"
```

### STT Example (JavaScript)

```javascript
const formData = new FormData();
formData.append('file', audioBlob, 'recording.webm');
formData.append('language', 'en');   // optional

const res = await fetch('https://api.yourdomain.com/speech-to-text', {
  method: 'POST',
  headers: { 'X-Api-Key': userApiKey },
  body: formData,
});
const data = await res.json();
console.log(data.detail);  // transcript
```

---

## Step 9 — Connecting TTS + STT Engines

The gateway proxies requests to your engine servers.

### TTS Engine Contract
```
POST {TTS_ENGINE_URL}/v1/tts
Content-Type: application/json
Body: { "text": "...", "language": "en", "voice": "rohit" }
Returns: audio/wav binary
```

### STT Engine Contract
```
POST {STT_ENGINE_URL}/v1/stt
Content-Type: multipart/form-data
Fields: file (audio), language (optional string)
Returns: { "text": "transcript..." }
         or plain text
```

### Test Engines Directly

```bash
# TTS
curl -X POST http://185.14.252.20:8000/v1/tts \
  -H "Content-Type: application/json" \
  -d '{"text":"test","language":"en","voice":"aria"}' \
  --output test.wav

# Via gateway
curl https://api.yourdomain.com/engine-health
# → {"tts":{"status":"ok"},"stt":{"status":"ok"}}
```

---

## Step 10 — Verification Checklist

```bash
# Server health
curl https://api.yourdomain.com/health
# → {"status":"ok"}

# Database connected
curl https://api.yourdomain.com/ready
# → {"status":"ready"}

# Engines reachable
curl https://api.yourdomain.com/engine-health
# → {"tts":{"status":"ok"},"stt":{"status":"ok"}}

# Create user
curl -X POST https://api.yourdomain.com/signup \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"Test1234!"}'
# → {"user_id":1,"api_key":"vgw_..."}

# Test TTS (audio_url should be an S3 HTTPS link)
curl -X POST https://api.yourdomain.com/text-to-speech \
  -H "X-Api-Key: vgw_YOUR_KEY" \
  -F "text=Hello World" -F "voice=aria"
# → {"audio_url":"https://...s3...amazonaws.com/tts/1.wav"}
```

---

## Environment Variables Reference

| Variable | Required | Example | Description |
|---|---|---|---|
| `ENVIRONMENT` | ✅ | `production` | Enables strict validation |
| `DATABASE_URL` | ✅ | `postgresql+psycopg2://...` | PostgreSQL connection |
| `JWT_SECRET` | ✅ | 64-char random string | Signs JWT tokens |
| `ALLOWED_ORIGINS` | ✅ | `https://yourapp.com` | Frontend CORS domain(s) |
| `TTS_ENGINE_URL` | ✅ | `http://185.14.252.20:8000` | TTS engine host |
| `STT_ENGINE_URL` | ✅ | `http://185.14.252.20:8002` | STT engine host |
| `USE_S3_STORAGE` | ✅ | `true` | Enable S3 audio storage |
| `AWS_ACCESS_KEY_ID` | if S3 | `AKIAxx...` | AWS IAM key |
| `AWS_SECRET_ACCESS_KEY` | if S3 | `xx...` | AWS IAM secret |
| `AWS_S3_BUCKET` | if S3 | `voice-gateway-audio` | S3 bucket name |
| `AWS_S3_REGION` | if S3 | `ap-south-1` | S3 bucket region |
| `CREATE_DB_TABLES` | ✅ | `false` | Always false in production |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError: boto3` | `pip install boto3==1.35.0` |
| `password authentication failed` | Check `DATABASE_URL` in `.env`; verify RDS security group allows your IP |
| `TTS engine unreachable` | `curl http://YOUR_TTS_IP:8000` — check firewall |
| CORS errors in frontend | Add frontend domain to `ALLOWED_ORIGINS` |
| Audio URL is `/audio/...` not S3 | Set `USE_S3_STORAGE=true` + all `AWS_*` vars |
| `JWT_SECRET must be set` | Generate: `python3 -c "import secrets; print(secrets.token_hex(32))"` |

---

## File Structure

```
voice-gateway/
├── api/index.py          ← Vercel demo only (no auth, no DB)
├── app/
│   ├── main.py           ← Production FastAPI app (runs on YOUR server)
│   ├── config.py         ← All env var settings
│   ├── database.py       ← SQLAlchemy → PostgreSQL
│   ├── models/           ← users, text_to_speech, speech_to_text
│   ├── routers/
│   │   ├── auth.py       ← /signup, /login
│   │   ├── tts.py        ← /text-to-speech (auth + DB + S3)
│   │   └── stt.py        ← /speech-to-text (auth + DB + S3)
│   └── storage/
│       └── audio_store.py← S3 or local disk
├── alembic/              ← DB migrations
├── .env                  ← Your secrets (never commit)
├── .env.example          ← Template
├── DEPLOYMENT.md         ← This file
├── requirements.txt
├── run.py
└── Dockerfile
```

---

## Removing the Demo (Production Hardening)

The project ships with two sets of demo endpoints — **disable both in production**.

### What Demo Routes Exist

| Source | Routes | Auth? | Purpose |
|---|---|---|---|
| `api/index.py` | `/api/tts`, `/api/stt`, `/api/voices`, `/api/health` | None | Vercel-only lightweight demo |
| `app/routers/demo.py` | `/demo`, `/demo/tts`, `/demo/stt`, `/demo/voices`, `/demo/health` | None | In-app unauthenticated test UI |

Both proxy directly to your TTS/STT engines without any authentication or rate limiting.

---

### Step 1 — Disable the In-App Demo Router (`/demo/*`)

The `/demo/*` routes are loaded conditionally in `app/main.py`.  
To remove them entirely, add this single line to your `.env`:

```bash
DISABLE_DEMO=true
```

Verify it's gone:
```bash
curl https://api.yourdomain.com/demo/tts
# → {"detail":"Not Found"}
```

---

### Step 2 — The Vercel `api/index.py` Does Not Run on Your Server

`api/index.py` is **only used by Vercel**. When you run via Docker or `python run.py`,
the server runs `app/main.py` exclusively — `api/index.py` is never touched.

> To also shut down the Vercel demo, delete the project in the
> [Vercel Dashboard](https://vercel.com/dashboard), or remove `vercel.json` from the repo.

---

### Step 3 — Lock Down CORS to Your Frontend

Add your real frontend domain to `.env`:

```bash
ALLOWED_ORIGINS=https://yourfrontend.com,https://www.yourfrontend.com
```

> **Never use `*` in production** — it allows any website to make API calls on behalf of your users.

---

### Step 4 — Full Production `.env` (Demo Disabled)

```bash
# ── App ───────────────────────────────────────────────────────────────────────
ENVIRONMENT=production
LOG_LEVEL=INFO
HOST=0.0.0.0
PORT=8001
CREATE_DB_TABLES=false

# ── DEMO OFF ──────────────────────────────────────────────────────────────────
DISABLE_DEMO=true

# ── Database ──────────────────────────────────────────────────────────────────
DATABASE_URL=postgresql+psycopg2://voicegw:YOUR_PASSWORD@YOUR_RDS_ENDPOINT:5432/voice_gateway

# ── Auth ──────────────────────────────────────────────────────────────────────
JWT_SECRET=your-64-char-random-secret-here
JWT_EXPIRES=3600

# ── CORS ──────────────────────────────────────────────────────────────────────
ALLOWED_ORIGINS=https://yourfrontend.com

# ── Voice Engines ─────────────────────────────────────────────────────────────
TTS_ENGINE_URL=http://185.14.252.20:8000
TTS_ENGINE_PATH=/v1/tts
TTS_ALLOWED_FORMATS=wav
MAX_TTS_TEXT_CHARS=500
STT_ENGINE_URL=http://185.14.252.20:8002
STT_ENGINE_PATH=/v1/stt
ENGINE_TIMEOUT_SECONDS=60

# ── Audio Storage (S3) ────────────────────────────────────────────────────────
MAX_AUDIO_UPLOAD_BYTES=5242880
ALLOWED_AUDIO_CONTENT_TYPES=audio/wav,audio/wave,audio/x-wav,audio/mpeg,audio/mp3,audio/mp4,audio/x-m4a,audio/webm,audio/ogg
USE_S3_STORAGE=true
AWS_ACCESS_KEY_ID=AKIAxxxxxxxxxxxxxxxx
AWS_SECRET_ACCESS_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
AWS_S3_BUCKET=voice-gateway-audio
AWS_S3_REGION=ap-south-1
```

---

### Demo vs Production — Side-by-Side

| Feature | Vercel Demo (`api/index.py`) | Production Server (`app/main.py`) |
|---|---|---|
| Authentication | None | API Key (`X-Api-Key` header) |
| Database | None | PostgreSQL (AWS RDS) |
| Audio storage | Streamed only, no persistence | AWS S3 — permanent HTTPS URLs |
| Usage tracking | None | Per-user request counts in DB |
| CORS | Open (`*`) | Locked to `ALLOWED_ORIGINS` |
| Demo routes | All endpoints are demo | Removed via `DISABLE_DEMO=true` |
| Deployment | Vercel serverless | Docker / any VPS |

---

### Security Checklist Before Going Live

- [ ] `DISABLE_DEMO=true` in `.env`
- [ ] `ENVIRONMENT=production`
- [ ] `ALLOWED_ORIGINS` is your specific frontend domain, not `*`
- [ ] `JWT_SECRET` is a 64-character random string (not the dev placeholder)
- [ ] `CREATE_DB_TABLES=false` — tables managed by Alembic only
- [ ] `.env` file is in `.gitignore` and has never been pushed to Git
- [ ] RDS security group only allows your server IP on port 5432
- [ ] HTTPS is active on your API domain (Let's Encrypt / ACM)
- [ ] `alembic upgrade head` has run successfully against PostgreSQL
- [ ] `GET /ready` returns `{"status":"ready"}`
- [ ] `GET /engine-health` returns `{"tts":{"status":"ok"},"stt":{"status":"ok"}}`
