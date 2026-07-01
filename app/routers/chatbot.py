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

import logging

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.dependencies import verify_api_key
from app.database import get_db
from app.models.user import User
from app.models.conversation import Conversation, ChatMessage
from app.services.rate_limiter import check_rate_limit

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chatbot"])
settings = get_settings()

LLM_SERVICE_URL = settings.llm_service_url.rstrip("/")
LLM_TIMEOUT = settings.llm_service_timeout
LLM_SERVICE_API_KEY = settings.llm_service_api_key

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
_STRIP_REQUEST_HEADERS = _HOP_BY_HOP | {"x-api-key", "x-service-key", "accept-encoding"}


def require_llm_access(
    x_api_key: str = Header(default=None),
    db: Session = Depends(get_db),
) -> User | None:
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
    return verify_api_key(x_api_key=x_api_key, db=db)

def _save_chat_messages(
    db: Session,
    user_id: int,
    user_content: str,
    assistant_content: str,
) -> None:
    """Create a conversation and save user + assistant messages. Never raises."""
    try:
        conv = Conversation(user_id=user_id, title="New Chat", mode="chat")
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


def _enforce_rate_limit(user: User | None, label: str, db: Session) -> None:
    """Per-user rate limit on the model (skipped when keyless or disabled)."""
    if user and settings.llm_rate_limit_enabled:
        check_rate_limit(user.user_id, label, db)


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
        async for chunk in upstream.aiter_raw():
            response_chunks.append(chunk)
            yield chunk
        await upstream.aclose()

        # Extract assistant content and save both messages
        if user and user_content:
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
                        db=db,
                        user_id=user.user_id,
                        user_content=user_content,
                        assistant_content=assistant_content,
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
