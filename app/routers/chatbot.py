"""LLM service proxy — exposes the chatbot LLM microservice through the gateway.

The gateway does NOT implement chat/translation/TTS itself: the chatbot project
is a separate microservice that talks to the model and does TTS/translation. This
router transparently reverse-proxies the LLM service and layers the gateway's
API-key management on top, so access is managed centrally (e.g. handing an
external user an API key to use our model through the gateway).

Proxied surface (forwarded verbatim to LLM_SERVICE_URL):
  POST /api/chat            — LLM chat (streaming SSE + non-streaming)
  POST /api/translate       — translation: 'llm' (AI model) or 'api' (free Google)
  POST /api/voice/tts       — Microsoft Edge/Bing neural TTS (streaming MP3)
  POST /api/voice/stt       — speech-to-text
  GET  /api/engine-health, /api/models, /api/health
  GET  /v1/models, POST /v1/chat/completions — OpenAI-compatible public API

Streaming is preserved end-to-end, so chat SSE and TTS audio start playing
immediately. Any new route the LLM service adds is proxied automatically.
"""

import asyncio
import logging

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from dataclasses import dataclass

from app.config import get_settings
from app.core.dependencies import verify_api_key
from app.core.ttl_cache import TTLCache
from app.database import get_db, SessionLocal
from app.models.user import User
from app.models.conversation import Conversation, ChatMessage
from app.services.rate_limiter import check_rate_limit_in_memory

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chatbot"])
settings = get_settings()

LLM_SERVICE_URL = settings.llm_service_url.rstrip("/")
LLM_TIMEOUT = settings.llm_service_timeout
LLM_SERVICE_API_KEY = settings.llm_service_api_key

# Chat/translate/live-translate all sit on this same LLM-service-proxy
# subsystem and share one bottleneck: every request/connection was
# re-validating the API key against Postgres, which is in a different AWS
# region from this gateway — each lookup pays a full cross-region round-trip
# even though a key's validity essentially never changes within a minute.
# This cache removes that repeated cost; TTS/STT's own routers are untouched.
@dataclass(slots=True)
class _CachedUser:
    """Minimal stand-in for User — only .user_id is read on the cached path."""
    user_id: int


_api_key_user_cache: TTLCache[int] = TTLCache(ttl_seconds=60)


def _resolve_user_by_api_key(api_key: str, db: Session) -> User | _CachedUser:
    """Validate an API key, using a short-lived cache to skip the DB when possible."""

    cached_user_id = _api_key_user_cache.get(api_key)
    if cached_user_id is not None:
        return _CachedUser(user_id=cached_user_id)

    user = verify_api_key(x_api_key=api_key, db=db)
    _api_key_user_cache.set(api_key, user.user_id)
    return user

# One shared, connection-pooled client for all proxied traffic — avoids the
# per-request socket/TLS handshake cost and lets the gateway scale to high
# concurrency. Created lazily inside the running event loop.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=LLM_TIMEOUT,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )
    return _client


async def aclose_client() -> None:
    """Close the shared client on app shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None

# Hop-by-hop / connection headers must not be forwarded in either direction.
_HOP_BY_HOP = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "te", "trailer", "upgrade", "proxy-authorization", "proxy-authenticate",
}
# The gateway API key is consumed here — never leak it to the upstream service.
# Drop accept-encoding so the upstream replies uncompressed: we stream raw bytes
# straight through, so a gzipped body (with the header stripped) would corrupt.
_STRIP_REQUEST_HEADERS = _HOP_BY_HOP | {
    "x-api-key", "x-service-key", "accept-encoding",
    # Gateway-only chat-history hints — meaningless to the upstream LLM service.
    "x-conversation-id", "x-client-persist",
}


def require_llm_access(
    x_api_key: str = Header(default=None),
    db: Session = Depends(get_db),
) -> User | _CachedUser | None:
    """Gate the proxied LLM routes with the gateway's API-key management.

    When llm_require_api_key is True, a valid X-API-Key (mapped to a User) is
    required — this is how external consumers use our model through the gateway.
    When False, the routes are open (e.g. for the chatbot's own frontend).
    """
    if not settings.llm_require_api_key:
        return None
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Send your gateway key in the X-API-Key header.",
        )
    return _resolve_user_by_api_key(x_api_key, db)


def _save_chat_messages(
    user_id: int,
    user_content: str,
    assistant_content: str,
    conversation_id: int | None = None,
) -> None:
    """Save user + assistant messages, appending to an existing conversation
    when the client sent X-Conversation-Id (and it belongs to this user),
    otherwise creating a new conversation titled after the first message.

    Opens its own DB session so it is safe to call from a streaming generator
    (where the request-scoped session from ``Depends(get_db)`` is already closed).
    Never raises — errors are logged and the session is rolled back.
    """
    from sqlalchemy.sql import func as _func

    db = SessionLocal()
    try:
        conv = None
        if conversation_id is not None:
            conv = (
                db.query(Conversation)
                .filter(
                    Conversation.conversation_id == conversation_id,
                    Conversation.user_id == user_id,
                )
                .first()
            )
        if conv:
            # Keep list ordering fresh — this conversation just saw activity.
            conv.updated_at = _func.now()
        else:
            title = (user_content.strip()[:80] or "New Chat")
            conv = Conversation(user_id=user_id, title=title, mode="chat")
            db.add(conv)
            db.flush()  # get conversation_id without committing yet

        db.add(ChatMessage(
            conversation_id=conv.conversation_id,
            user_id=user_id,
            role="user",
            content=user_content,
            engine="llm",
        ))
        db.add(ChatMessage(
            conversation_id=conv.conversation_id,
            user_id=user_id,
            role="assistant",
            content=assistant_content,
            engine="llm",
        ))
        db.commit()
        logger.info("chat_saved conv=%s user=%s", conv.conversation_id, user_id)
    except Exception as exc:
        logger.error("chat_save_failed: %s", str(exc))
        db.rollback()
    finally:
        db.close()


async def _proxy(request: Request, upstream_path: str) -> StreamingResponse:
    """Transparently forward a request to the LLM service and stream the reply.

    Works uniformly for JSON, form, and multipart bodies (the raw body and its
    Content-Type are forwarded as-is) and preserves streaming responses (SSE
    chat chunks, MP3 audio) by piping the upstream body through untouched.
    """
    url = f"{LLM_SERVICE_URL}{upstream_path}"
    body = await request.body()
    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _STRIP_REQUEST_HEADERS
    }
    # Authenticate the gateway → LLM service hop so direct-IP bypass is rejected.
    if LLM_SERVICE_API_KEY:
        fwd_headers["X-Service-Key"] = LLM_SERVICE_API_KEY

    client = _get_client()
    try:
        upstream_req = client.build_request(
            request.method,
            url,
            content=body,
            headers=fwd_headers,
            params=dict(request.query_params),
        )
        upstream = await client.send(upstream_req, stream=True)
    except httpx.RequestError as exc:
        logger.error("LLM service unreachable: %s %s — %s", request.method, url, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM service unreachable: {exc}",
        )

    async def stream_body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()   # close the response, keep the pooled client

    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "content-encoding"
    }
    return StreamingResponse(
        stream_body(),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )


def _enforce_rate_limit(user: "User | _CachedUser | None", label: str, db: Session) -> None:
    """Per-user rate limit on the model (skipped when keyless or disabled).

    Uses the in-memory limiter, not the DB-backed one: this subsystem (chat +
    translate) always passes label="llm", and the DB round-trip for two extra
    queries (RPM + RPD) per request was a major contributor to live-translate
    latency given Postgres's cross-region distance from this gateway. See
    check_rate_limit_in_memory()'s docstring for the persistence trade-off.
    """
    if user and settings.llm_rate_limit_enabled:
        check_rate_limit_in_memory(user.user_id, label)


# Catch-all proxy: every /api/* and /v1/* route on the LLM service is forwarded,
# gated by the gateway's API-key management + optional per-user rate limiting.
@router.post("/api/chat")
async def proxy_chat(
    request: Request,
    user=Depends(require_llm_access),
    db: Session = Depends(get_db),
):
    """Proxy /api/chat and save user + assistant messages to DB."""
    import json as _json

    _enforce_rate_limit(user, "llm", db)

    # Optional gateway-only header: continue an existing conversation instead
    # of creating a new one per turn (ownership is verified at save time).
    try:
        conversation_id = int(request.headers.get("x-conversation-id", ""))
    except ValueError:
        conversation_id = None

    # Clients that persist chats themselves (via /conversations) send
    # X-Client-Persist so the gateway doesn't double-save the exchange.
    client_persists = request.headers.get("x-client-persist") is not None

    # Read body once — needed for both DB saving and forwarding
    body_bytes = await request.body()

    # Extract user message from request body
    user_content = ""
    try:
        body_data = _json.loads(body_bytes)
        messages = body_data.get("messages", [])
        user_msgs = [m for m in messages if m.get("role") == "user"]
        if user_msgs:
            user_content = user_msgs[-1].get("content", "")
    except Exception:
        pass

    # Forward to LLM service — collect full response for non-streaming
    url = f"{LLM_SERVICE_URL}/api/chat"
    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _STRIP_REQUEST_HEADERS
    }
    if LLM_SERVICE_API_KEY:
        fwd_headers["X-Service-Key"] = LLM_SERVICE_API_KEY

    client = _get_client()
    try:
        upstream_req = client.build_request(
            "POST", url,
            content=body_bytes,
            headers=fwd_headers,
        )
        upstream = await client.send(upstream_req, stream=True)
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM service unreachable: {exc}",
        )

    # Collect response to extract assistant content for DB saving
    response_chunks = []

    async def stream_and_save():
        # finally ensures the pooled connection is released even if the client
        # disconnects mid-stream; the save below only runs on full completion.
        try:
            async for chunk in upstream.aiter_raw():
                response_chunks.append(chunk)
                yield chunk
        finally:
            await upstream.aclose()

        # Extract assistant content and save both messages.
        # NOTE: we use _save_chat_messages (which opens its own session)
        # because the request-scoped `db` is closed by FastAPI before this
        # generator finishes.
        if user and user_content and not client_persists:
            try:
                full_text = b"".join(response_chunks).decode("utf-8", errors="ignore")
                assistant_content = ""

                # Try JSON parse first (non-streaming)
                try:
                    data = _json.loads(full_text)
                    choices = data.get("choices", [])
                    if choices:
                        assistant_content = choices[0].get("message", {}).get("content", "")
                except Exception:
                    pass

                # Try SSE parse (streaming)
                if not assistant_content:
                    parts = []
                    for line in full_text.splitlines():
                        if line.startswith("data:") and "[DONE]" not in line:
                            try:
                                d = _json.loads(line[5:].strip())
                                parts.append(d.get("content", ""))
                            except Exception:
                                pass
                    assistant_content = "".join(parts)

                if assistant_content:
                    _save_chat_messages(
                        user_id=user.user_id,
                        user_content=user_content,
                        assistant_content=assistant_content,
                        conversation_id=conversation_id,
                    )
            except Exception as exc:
                logger.error("chat_db_save_error: %s", str(exc))

    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "content-encoding"
    }
    return StreamingResponse(
        stream_and_save(),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )


# ── Live voice STT — straight to the STT engine ──────────────────────────────
# Live voice translation posts a mic chunk every ~2 seconds. Routing each one
# UI → gateway → LLM service → STT engine spent a whole extra proxy hop per
# chunk just to reach an engine whose address the gateway already knows
# (STT_ENGINE_URL). This route calls the engine directly and asks it to skip
# word-timestamp alignment (timestamps=false) — a large slice of CPU inference
# time that subtitles never use. Response shape matches the LLM-service proxy
# ({text, language, words}) so clients need no changes, and an engine that
# doesn't know the `timestamps` field yet simply ignores it.
# MUST be defined BEFORE the /api/{path:path} catch-all to win the route match.
@router.post("/api/voice/stt")
async def voice_stt_fast(
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
    user=Depends(require_llm_access),
    db: Session = Depends(get_db),
):
    _enforce_rate_limit(user, "llm", db)
    data = await file.read()
    filename = file.filename or "chunk.webm"
    content_type = file.content_type or "audio/webm"

    form: dict[str, str] = {"timestamps": "false"}
    if language:
        form["language"] = language

    headers: dict[str, str] = {}
    if settings.stt_engine_url:
        url = f"{settings.stt_engine_url.rstrip('/')}{settings.stt_engine_path}"
    else:
        # No direct engine configured — fall back to the LLM-service hop.
        url = f"{LLM_SERVICE_URL}/api/voice/stt"
        if LLM_SERVICE_API_KEY:
            headers["X-Service-Key"] = LLM_SERVICE_API_KEY

    client = _get_client()
    try:
        # Live chunks are ~2s of audio; a healthy engine answers in well under
        # a second. Don't inherit the shared client's 120s LLM timeout — a
        # hung chunk would pin the UI's in-flight slot and stall the stream.
        resp = await client.post(
            url,
            files={"file": (filename, data, content_type)},
            data=form,
            headers=headers,
            timeout=httpx.Timeout(15.0, connect=5.0),
        )
    except httpx.RequestError as exc:
        logger.error("voice_stt_fast: engine unreachable at %s — %s", url, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"STT engine unreachable: {exc}",
        )

    if resp.status_code != 200:
        logger.warning("voice_stt_fast: engine returned %s", resp.status_code)
        raise HTTPException(
            status_code=resp.status_code,
            detail=resp.text[:300] or "STT engine error",
        )

    payload = resp.json()
    return {
        "text": payload.get("text", ""),
        "language": payload.get("language"),
        "words": payload.get("words", []),
    }


@router.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def proxy_api(
    path: str,
    request: Request,
    user=Depends(require_llm_access),
    db: Session = Depends(get_db),
):
    _enforce_rate_limit(user, "llm", db)
    return await _proxy(request, f"/api/{path}")


# ── Live translation WebSocket proxy ─────────────────────────────────────────
# Bridges the client's WebSocket to the LLM service's /ws/translate so typed
# text is translated live (token-by-token) through the gateway. Browsers cannot
# set headers on WebSockets, so the gateway API key is accepted via the
# ?api_key= query param (an X-API-Key header also works for non-browser
# clients). The upstream hop is authenticated with the service key.


def _ws_upstream_url() -> str:
    base = LLM_SERVICE_URL.replace("https://", "wss://").replace("http://", "ws://")
    url = f"{base}/ws/translate"
    if LLM_SERVICE_API_KEY:
        url += f"?service_key={LLM_SERVICE_API_KEY}"
    return url


@router.websocket("/ws/translate")
async def ws_translate_proxy(websocket: WebSocket):
    """Proxy the live-translation WebSocket, gated by gateway API keys."""
    # Accept FIRST: closing before accept() is sent as a bare HTTP 403, so the
    # browser would see a generic handshake failure (1006) instead of our close
    # codes (4401 bad key / 1011 upstream down) and couldn't show why.
    await websocket.accept()

    if settings.llm_require_api_key:
        api_key = (
            websocket.query_params.get("api_key")
            or websocket.headers.get("x-api-key")
        )
        if not api_key:
            await websocket.close(code=4401, reason="Missing API key")
            return
        db = SessionLocal()
        try:
            user = _resolve_user_by_api_key(api_key, db)
        except HTTPException:
            user = None
        finally:
            db.close()
        if not user:
            await websocket.close(code=4401, reason="Invalid API key")
            return

    try:
        import websockets
    except ImportError:
        logger.error("ws_translate: websockets package not installed on gateway")
        await websocket.close(code=1011, reason="websockets package not installed")
        return

    upstream_url = _ws_upstream_url()
    try:
        upstream = await websockets.connect(upstream_url, max_size=2**20, open_timeout=10)
    except Exception as exc:
        # Fallback to the alternative endpoint shape:
        # If upstream_url is /ws/translate, try /api/translate/ws
        # If upstream_url is /api/translate/ws, try /ws/translate
        alt_url = upstream_url
        if "/ws/translate" in upstream_url:
            alt_url = upstream_url.replace("/ws/translate", "/api/translate/ws")
        elif "/api/translate/ws" in upstream_url:
            alt_url = upstream_url.replace("/api/translate/ws", "/ws/translate")
        
        if alt_url != upstream_url:
            logger.info("Retrying LLM service WS with fallback URL: %s", alt_url.split("?")[0])
            try:
                upstream = await websockets.connect(alt_url, max_size=2**20, open_timeout=10)
            except Exception as exc2:
                logger.error("LLM service WS unreachable at both %s and %s: %s", 
                             upstream_url.split("?")[0], alt_url.split("?")[0], exc2)
                await websocket.close(code=1011, reason="LLM service unreachable")
                return
        else:
            logger.error("LLM service WS unreachable at %s: %s", upstream_url.split("?")[0], exc)
            await websocket.close(code=1011, reason="LLM service unreachable")
            return

    async def client_to_upstream():
        while True:
            data = await websocket.receive_text()
            await upstream.send(data)

    async def upstream_to_client():
        async for message in upstream:
            if isinstance(message, bytes):
                await websocket.send_bytes(message)
            else:
                await websocket.send_text(message)

    pump_up = asyncio.create_task(client_to_upstream())
    pump_down = asyncio.create_task(upstream_to_client())
    try:
        done, pending = await asyncio.wait(
            {pump_up, pump_down}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect,)):
                logger.info("ws_translate proxy closed: %s", exc)
    finally:
        await upstream.close()
        try:
            await websocket.close()
        except Exception:
            pass


# ── Live voice-translation WebSocket ─────────────────────────────────────────
# Bridges the browser's microphone to the STT engine + LLM translation pipeline.
#
# Flow:
#   1. Client opens WS, sends {"type":"config", "target_lang":"es"} once.
#   2. Client streams binary audio chunks (MediaRecorder/webm, ~2 s each).
#   3. Gateway POSTs each chunk to the STT service (/api/voice/stt).
#   4. STT transcript is forwarded over the upstream /ws/translate connection.
#   5. Translation delta/done frames stream back to the browser in real-time.
#
# Client receives:
#   {"type":"transcript","text":"Hello world"}      – what STT heard
#   {"type":"delta","content":"Hola ","id":N}       – streaming translation token
#   {"type":"done","translation":"Hola mundo","id":N}
#   {"type":"error","message":"..."}


@router.websocket("/ws/voice-translate")
async def ws_voice_translate(websocket: WebSocket):
    """Live voice-translation: mic audio → STT → streaming translate."""
    await websocket.accept()

    # ── Auth ──────────────────────────────────────────────────────────────────
    if settings.llm_require_api_key:
        api_key = (
            websocket.query_params.get("api_key")
            or websocket.headers.get("x-api-key")
        )
        if not api_key:
            await websocket.close(code=4401, reason="Missing API key")
            return
        db = SessionLocal()
        try:
            user = _resolve_user_by_api_key(api_key, db)
        except HTTPException:
            user = None
        finally:
            db.close()
        if not user:
            await websocket.close(code=4401, reason="Invalid API key")
            return
    else:
        user = None

    if settings.llm_rate_limit_enabled and user:
        try:
            check_rate_limit_in_memory(user.user_id, "llm")
        except HTTPException as exc:
            await websocket.send_text(
                f'{{"type":"error","message":"{exc.detail}"}}'
            )
            await websocket.close(code=1008, reason="Rate limit exceeded")
            return

    try:
        import websockets as _ws_lib
    except ImportError:
        logger.error("ws_voice_translate: websockets package not installed")
        await websocket.close(code=1011, reason="websockets package not installed")
        return

    # ── Connect to upstream translate WebSocket ───────────────────────────────
    upstream_url = _ws_upstream_url()
    try:
        upstream = await _ws_lib.connect(
            upstream_url, max_size=2 ** 20, open_timeout=10
        )
    except Exception as exc:
        # Try the alternate URL shape (same fallback logic as ws_translate_proxy)
        alt_url = upstream_url
        if "/ws/translate" in upstream_url:
            alt_url = upstream_url.replace("/ws/translate", "/api/translate/ws")
        elif "/api/translate/ws" in upstream_url:
            alt_url = upstream_url.replace("/api/translate/ws", "/ws/translate")
        try:
            upstream = await _ws_lib.connect(
                alt_url, max_size=2 ** 20, open_timeout=10
            )
        except Exception as exc2:
            logger.error(
                "ws_voice_translate: upstream unreachable — %s / %s", exc, exc2
            )
            await websocket.close(code=1011, reason="Translation service unreachable")
            return

    # STT service: prefer the LLM service proxy route so it works the same as
    # the existing /api/voice/stt proxy in chatbot.py.
    stt_url = f"{LLM_SERVICE_URL}/api/voice/stt"
    stt_headers: dict[str, str] = {}
    if LLM_SERVICE_API_KEY:
        stt_headers["X-Service-Key"] = LLM_SERVICE_API_KEY

    target_lang = "en"   # will be updated on first config frame
    seq = 0
    client = _get_client()

    async def _send_audio_chunk(audio_bytes: bytes) -> None:
        """POST a raw audio chunk to the STT service, forward transcript to upstream."""
        nonlocal seq, target_lang
        try:
            import httpx as _httpx
            files = {"file": ("chunk.webm", audio_bytes, "audio/webm")}
            resp = await client.post(stt_url, files=files, headers=stt_headers, timeout=30)
            if not resp.is_success:
                logger.warning("STT returned %s for voice chunk", resp.status_code)
                return

            data = resp.json()
            # LLM-service STT: {"text": "...", "language": "..."}
            # Gateway STT:     {"detail": "...", "request_id": ...}
            transcript = (
                data.get("text")
                or data.get("detail")
                or data.get("transcript")
                or ""
            )
            if not transcript or not transcript.strip():
                return

            # Echo transcript to client so the source panel updates
            await websocket.send_text(
                f'{{"type":"transcript","text":{__import__("json").dumps(transcript)}}}'
            )

            # Forward to upstream translate WS
            seq += 1
            payload = __import__("json").dumps({
                "type": "translate",
                "id": seq,
                "text": transcript.strip(),
                "target_lang": target_lang,
                "engine": "llm",
            })
            await upstream.send(payload)

        except Exception as exc:
            logger.warning("voice-translate chunk error: %s", exc)
            try:
                await websocket.send_text(
                    f'{{"type":"error","message":"Chunk processing failed: {exc}"}}'
                )
            except Exception:
                pass

    async def _pump_upstream_to_client() -> None:
        """Forward translation delta/done/error frames from upstream to client."""
        try:
            async for message in upstream:
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_text(message)
        except Exception:
            pass

    async def _pump_client_to_upstream() -> None:
        """Read mic audio chunks / control frames from the browser."""
        nonlocal target_lang
        import json as _json
        while True:
            message = await websocket.receive()
            msg_type = message.get("type")

            if msg_type == "websocket.disconnect":
                break

            if msg_type == "websocket.receive":
                binary = message.get("bytes")
                text = message.get("text")

                if binary:
                    # Raw audio chunk — send to STT → translate
                    asyncio.create_task(_send_audio_chunk(binary))

                elif text:
                    try:
                        frame = _json.loads(text)
                    except ValueError:
                        continue

                    if frame.get("type") == "config":
                        target_lang = frame.get("target_lang", target_lang)
                    elif frame.get("type") == "ping":
                        await websocket.send_text('{"type":"pong"}')
                    # Ignore other text frames

    pump_down = asyncio.create_task(_pump_upstream_to_client())
    try:
        await _pump_client_to_upstream()
    except Exception as exc:
        logger.info("ws_voice_translate client loop ended: %s", exc)
    finally:
        pump_down.cancel()
        try:
            await upstream.close()
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass


@router.api_route(
    "/v1/{path:path}",
    methods=["GET", "POST"],
)
async def proxy_v1(
    path: str,
    request: Request,
    user=Depends(require_llm_access),
    db: Session = Depends(get_db),
):
    _enforce_rate_limit(user, "llm", db)
    return await _proxy(request, f"/v1/{path}")
