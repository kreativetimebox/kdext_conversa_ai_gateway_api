"""Vercel serverless entry point — lightweight demo proxy, no database required.

All demo endpoints are public (no auth). This is what Vercel runs.
The full DB-backed app (app/main.py) is used for self-hosted / Docker deployments.

Routes exposed on Vercel:
  GET  /api/health          — liveness check
  GET  /api/engine-health   — connectivity to TTS + STT engines
  GET  /api/voices          — list available TTS voices
  POST /api/tts             — proxy to TTS engine, returns raw audio bytes
  POST /api/stt             — proxy to STT engine, returns transcript JSON
  GET  /                    — serves the demo HTML page (public/index.html)
"""

import os
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response

# ── Engine URLs from environment ─────────────────────────────────────────────
TTS_ENGINE_URL  = os.environ.get("TTS_ENGINE_URL",  "http://185.14.252.20:8000")
TTS_ENGINE_PATH = os.environ.get("TTS_ENGINE_PATH", "/v1/tts")
STT_ENGINE_URL  = os.environ.get("STT_ENGINE_URL",  "http://185.14.252.20:8002")
STT_ENGINE_PATH = os.environ.get("STT_ENGINE_PATH", "/v1/stt")
ENGINE_TIMEOUT  = float(os.environ.get("ENGINE_TIMEOUT_SECONDS", "60"))

# ── Speaker voice map (no DB needed — hardcoded for the demo) ─────────────────
SPEAKERS = {
    "rohit":   {"gender": "male",   "style": "Conversational Hindi", "language": "hi"},
    "divya":   {"gender": "female", "style": "Expressive Hindi",     "language": "hi"},
    "aria":    {"gender": "female", "style": "Friendly English",     "language": "en"},
    "james":   {"gender": "male",   "style": "Professional English", "language": "en"},
    "sofia":   {"gender": "female", "style": "Warm Spanish",         "language": "es"},
    "aisha":   {"gender": "female", "style": "Expressive Arabic",    "language": "ar"},
}

# Demo HTML lives in public/index.html (served by Vercel as a static file,
# but we also serve it via FastAPI as a fallback)
_PUBLIC = Path(__file__).resolve().parent.parent / "public" / "index.html"

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Voice Gateway Demo", version="1.0.0", docs_url="/api/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/engine-health")
async def engine_health():
    """Connectivity check against both engines."""
    results = {}
    for label, url in [("tts", TTS_ENGINE_URL), ("stt", STT_ENGINE_URL)]:
        if not url:
            results[label] = {"status": "not_configured"}
            continue
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(url, timeout=5)
                results[label] = {"status": "ok", "http": r.status_code}
        except Exception as exc:
            results[label] = {"status": "unreachable", "error": str(exc)}
    return results


# ── Voices ─────────────────────────────────────────────────────────────────────

@app.get("/api/voices")
async def list_voices():
    """Return available TTS speaker voices — no auth required."""
    return {
        "voices": [
            {
                "id": name,
                "name": name.capitalize(),
                "gender": info["gender"],
                "style": info["style"],
                "language": info["language"],
            }
            for name, info in SPEAKERS.items()
        ],
        "usage": "Pass the voice 'id' as the `voice` field in POST /api/tts",
    }


# ── TTS ────────────────────────────────────────────────────────────────────────

@app.post("/api/tts")
async def demo_tts(
    text: str = Form(...),
    language: str = Form(default="en"),
    voice: str = Form(default="aria"),
):
    """Proxy TTS request to the engine and return raw audio bytes. No auth."""
    # Resolve a named speaker (e.g. 'rohit') to its natural-language description
    # if needed, otherwise pass the voice string straight through
    resolved_voice = voice
    if voice.lower() in SPEAKERS:
        resolved_voice = f"{SPEAKERS[voice.lower()]['style']} speaker"

    url = f"{TTS_ENGINE_URL.rstrip('/')}{TTS_ENGINE_PATH}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                json={"text": text, "language": language, "voice": resolved_voice},
                timeout=ENGINE_TIMEOUT,
            )
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "audio/wav")
            return Response(content=resp.content, media_type=content_type)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"TTS engine error: {exc.response.text[:500]}",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"TTS engine unreachable: {exc}",
        )


# ── STT ────────────────────────────────────────────────────────────────────────

@app.post("/api/stt")
async def demo_stt(
    file: UploadFile = File(...),
    language: str = Form(default=None),
):
    """Proxy STT request to the engine and return transcript. No auth."""
    if not STT_ENGINE_URL:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="STT engine is not configured",
        )

    url = f"{STT_ENGINE_URL.rstrip('/')}{STT_ENGINE_PATH}"
    data = await file.read()
    filename = file.filename or "audio.wav"
    # Strip codec params: 'audio/webm;codecs=opus' → 'audio/webm'
    raw_ct = file.content_type or "audio/wav"
    content_type = raw_ct.split(";")[0].strip()

    form_data = {}
    if language:
        form_data["language"] = language

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                files={"file": (filename, data, content_type)},
                data=form_data,
                timeout=ENGINE_TIMEOUT,
            )
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "")
            if "application/json" in ct:
                return resp.json()
            return {"text": resp.text.strip()}
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"STT engine error: {exc.response.text[:500]}",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"STT engine unreachable: {exc}",
        )


# ── Demo page fallback ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def demo_page():
    """Serve the demo HTML page."""
    if _PUBLIC.exists():
        return HTMLResponse(_PUBLIC.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Voice Gateway</h1><p>Demo page not found.</p>")
