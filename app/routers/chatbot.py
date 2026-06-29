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

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chatbot"])
settings = get_settings()

LLM_SERVICE_URL = settings.llm_service_url.rstrip("/")
LLM_TIMEOUT = settings.llm_service_timeout

# Hop-by-hop / connection headers must not be forwarded in either direction.
_HOP_BY_HOP = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "te", "trailer", "upgrade", "proxy-authorization", "proxy-authenticate",
}
# The gateway API key is consumed here — never leak it to the upstream service.
# Drop accept-encoding so the upstream replies uncompressed: we stream raw bytes
# straight through, so a gzipped body (with the header stripped) would corrupt.
_STRIP_REQUEST_HEADERS = _HOP_BY_HOP | {"x-api-key", "accept-encoding"}


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

    client = httpx.AsyncClient(timeout=LLM_TIMEOUT)
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
        await client.aclose()
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
            await upstream.aclose()
            await client.aclose()

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


# Catch-all proxy: every /api/* and /v1/* route on the LLM service is forwarded,
# gated by the gateway's API-key management.
@router.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def proxy_api(path: str, request: Request, _user=Depends(require_llm_access)):
    return await _proxy(request, f"/api/{path}")


@router.api_route(
    "/v1/{path:path}",
    methods=["GET", "POST"],
)
async def proxy_v1(path: str, request: Request, _user=Depends(require_llm_access)):
    return await _proxy(request, f"/v1/{path}")
