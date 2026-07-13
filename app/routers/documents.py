"""Document-chat endpoints — reference an OCR-scanned document, then ask about it.

Flow:
  1. POST /documents/reference {request_id}   — gateway fetches the scanned
     document from the OCR service (OCR_SERVICE_URL/v1/requests/{request_id}),
     extracts every piece of scanned text, caches it, returns a summary.
  2. GET  /documents/{request_id}             — full scanned text + metadata.
  3. POST /documents/{request_id}/chat        — question answered by the LLM
     service using ONLY the scanned document as context (SSE streaming
     preserved, exchange saved to chat history with mode='document').

Mounted at root level (like /conversations) so the LLM-proxy's /api/{path}
catch-all never captures these routes. Auth matches the LLM proxy: when
llm_require_api_key is on, a gateway X-API-Key is required.

The OCR service's response shape isn't pinned down, so text extraction walks
the payload defensively: it tries a priority list of well-known text keys
(markdown, text, ocr_text, ...) at any nesting depth and joins multi-page
results, instead of assuming one fixed schema.
"""

import json
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from app.config import get_settings
from app.core.ttl_cache import TTLCache
from app.database import get_db, SessionLocal
from app.models.conversation import ChatMessage, Conversation
from app.routers.chatbot import (
    LLM_SERVICE_API_KEY,
    LLM_SERVICE_URL,
    _enforce_rate_limit,
    _get_client,
    require_llm_access,
)
from app.schemas.documents import (
    DocumentChatIn,
    DocumentContentOut,
    DocumentOut,
    DocumentReferenceIn,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/documents", tags=["documents"])
settings = get_settings()

OCR_SERVICE_URL = settings.ocr_service_url.rstrip("/")

# request_id → {"text", "status", "filename", "pages"}. In-process + TTL'd:
# a follow-up question re-fetches from OCR at worst once per TTL per process.
_document_cache: TTLCache[dict] = TTLCache(
    ttl_seconds=settings.document_cache_ttl_seconds
)


# ── OCR payload parsing ───────────────────────────────────────────────────────

# Checked in priority order; the first key that yields text anywhere in the
# payload wins. Multiple hits for the same key (per-page results) are joined.
_TEXT_KEYS = (
    "markdown",
    "full_text",
    "extracted_text",
    "ocr_text",
    "raw_text",
    "text",
    "content",
    "transcription",
)
_FILENAME_KEYS = ("filename", "file_name", "original_filename", "document_name", "name")
_STATUS_KEYS = ("status", "state")
_PENDING_STATUSES = {"pending", "queued", "processing", "running", "in_progress"}


def _collect_key(node, key: str, out: list[str]) -> None:
    """Depth-first collect every non-empty string stored under `key`."""
    if isinstance(node, dict):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
        for k, v in node.items():
            if k != key:
                _collect_key(v, key, out)
    elif isinstance(node, list):
        for item in node:
            _collect_key(item, key, out)


def _find_str(node, keys: tuple[str, ...]) -> str | None:
    """Return the first string found under any of `keys`, shallowest-first."""
    queue = [node]
    while queue:
        current = queue.pop(0)
        if isinstance(current, dict):
            for key in keys:
                value = current.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            queue.extend(current.values())
        elif isinstance(current, list):
            queue.extend(current)
    return None


def _count_pages(payload) -> int | None:
    """Best-effort page count: an int page_count/num_pages, or len(pages)."""
    found = _find_int(payload, ("page_count", "num_pages", "total_pages"))
    if found is not None:
        return found
    queue = [payload]
    while queue:
        current = queue.pop(0)
        if isinstance(current, dict):
            pages = current.get("pages")
            if isinstance(pages, list) and pages:
                return len(pages)
            queue.extend(current.values())
        elif isinstance(current, list):
            queue.extend(current)
    return None


def _find_int(node, keys: tuple[str, ...]) -> int | None:
    queue = [node]
    while queue:
        current = queue.pop(0)
        if isinstance(current, dict):
            for key in keys:
                value = current.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    return value
            queue.extend(current.values())
        elif isinstance(current, list):
            queue.extend(current)
    return None


def extract_document(payload) -> dict:
    """Pull scanned text + metadata out of an OCR response of unknown shape."""
    text = ""
    for key in _TEXT_KEYS:
        chunks: list[str] = []
        _collect_key(payload, key, chunks)
        # Drop trivial hits (e.g. status text fields) — scanned content is long.
        chunks = [c for c in chunks if len(c) > 1]
        if chunks:
            text = "\n\n".join(chunks)
            break

    return {
        "text": text,
        "status": (_find_str(payload, _STATUS_KEYS) or "completed").lower(),
        "filename": _find_str(payload, _FILENAME_KEYS),
        "pages": _count_pages(payload),
    }


# ── OCR service fetch ─────────────────────────────────────────────────────────


async def _fetch_document(request_id: str, refresh: bool = False) -> tuple[dict, bool]:
    """Return (document, from_cache) for an OCR request id.

    Raises 404 (unknown id), 409 (scan not finished), 502 (OCR service down).
    """
    if not refresh:
        cached = _document_cache.get(request_id)
        if cached is not None:
            return cached, True

    url = f"{OCR_SERVICE_URL}/v1/requests/{request_id}"
    headers: dict[str, str] = {}
    if settings.ocr_service_api_key:
        headers["X-API-Key"] = settings.ocr_service_api_key
        headers["Authorization"] = f"Bearer {settings.ocr_service_api_key}"

    client = _get_client()
    try:
        resp = await client.get(
            url, headers=headers, timeout=settings.ocr_service_timeout
        )
    except httpx.RequestError as exc:
        logger.error("OCR service unreachable at %s — %s", url, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"OCR service unreachable: {exc}",
        )

    if resp.status_code == 404:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document request '{request_id}' not found on the OCR service",
        )
    if resp.status_code != 200:
        logger.warning("OCR service returned %s for %s", resp.status_code, request_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"OCR service error ({resp.status_code}): {resp.text[:300]}",
        )

    try:
        payload = resp.json()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="OCR service returned a non-JSON response",
        )

    doc = extract_document(payload)

    if not doc["text"]:
        # No text yet — distinguish "still scanning" from "scanned but empty".
        if doc["status"] in _PENDING_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Document is not ready yet (status: {doc['status']}). "
                       "Retry once the OCR scan completes.",
            )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="OCR scan contains no extractable text for this document",
        )

    _document_cache.set(request_id, doc)
    return doc, False


def _doc_summary(request_id: str, doc: dict, cached: bool) -> dict:
    return {
        "request_id": request_id,
        "status": doc["status"],
        "filename": doc["filename"],
        "pages": doc["pages"],
        "characters": len(doc["text"]),
        "preview": doc["text"][:400],
        "cached": cached,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/reference", response_model=DocumentOut)
async def reference_document(
    body: DocumentReferenceIn,
    user=Depends(require_llm_access),
    db: Session = Depends(get_db),
):
    """Register a document by OCR request id: fetch, scan-extract, and cache it.

    The UI calls this once when the user pastes/picks a document reference;
    afterwards questions go to POST /documents/{request_id}/chat.
    """
    doc, cached = await _fetch_document(body.request_id, refresh=body.refresh)
    return _doc_summary(body.request_id, doc, cached)


@router.get("/{request_id}", response_model=DocumentContentOut)
async def get_document(
    request_id: str,
    user=Depends(require_llm_access),
    db: Session = Depends(get_db),
):
    """Full scanned text + metadata (fetches from OCR if not cached)."""
    doc, cached = await _fetch_document(request_id)
    return {**_doc_summary(request_id, doc, cached), "text": doc["text"]}


@router.delete("/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
async def forget_document(
    request_id: str,
    user=Depends(require_llm_access),
):
    """Drop a document from the gateway cache (next reference re-fetches OCR)."""
    _document_cache.invalidate(request_id)


def _document_system_prompt(request_id: str, doc: dict) -> str:
    text = doc["text"][: settings.max_document_context_chars]
    name = doc["filename"] or request_id
    return (
        "You are a document assistant. The user has attached a scanned document "
        f"named '{name}'. Answer the user's questions using ONLY the document "
        "content below — everything the OCR scan extracted from it. If the "
        "answer is not present in the document, say so plainly instead of "
        "guessing. Quote the document where it helps.\n\n"
        "--- DOCUMENT CONTENT START ---\n"
        f"{text}\n"
        "--- DOCUMENT CONTENT END ---"
    )


def _save_document_chat(
    user_id: int,
    request_id: str,
    doc: dict,
    question: str,
    answer: str,
    conversation_id: int | None,
) -> None:
    """Persist the Q/A pair (mode='document'), like chatbot._save_chat_messages.

    Opens its own session — the request-scoped one is closed by the time a
    streaming generator finishes. Never raises.
    """
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
            conv.updated_at = func.now()
        else:
            name = doc["filename"] or request_id
            conv = Conversation(
                user_id=user_id,
                title=f"Doc: {name}"[:255],
                mode="document",
            )
            db.add(conv)
            db.flush()

        db.add(ChatMessage(
            conversation_id=conv.conversation_id,
            user_id=user_id,
            role="user",
            content=question,
            engine="llm",
        ))
        db.add(ChatMessage(
            conversation_id=conv.conversation_id,
            user_id=user_id,
            role="assistant",
            content=answer,
            engine="llm",
        ))
        db.commit()
        logger.info(
            "document_chat_saved conv=%s user=%s doc=%s",
            conv.conversation_id, user_id, request_id,
        )
    except Exception as exc:
        logger.error("document_chat_save_failed: %s", exc)
        db.rollback()
    finally:
        db.close()


@router.post("/{request_id}/chat")
async def chat_with_document(
    request_id: str,
    body: DocumentChatIn,
    user=Depends(require_llm_access),
    db: Session = Depends(get_db),
):
    """Answer a question from the scanned document via the LLM service.

    The document text is injected server-side as a system prompt; the client
    only ever sends the question (plus optional prior turns). The response is
    the LLM service's own /api/chat shape — SSE stream when body.stream is
    true, JSON otherwise — so the UI can reuse its existing chat handling.
    """
    _enforce_rate_limit(user, "llm", db)

    doc, _ = await _fetch_document(request_id)

    messages = [{"role": "system", "content": _document_system_prompt(request_id, doc)}]
    messages += [m.model_dump() for m in body.history]
    messages.append({"role": "user", "content": body.question})

    fwd_headers = {"Content-Type": "application/json"}
    if LLM_SERVICE_API_KEY:
        fwd_headers["X-Service-Key"] = LLM_SERVICE_API_KEY

    client = _get_client()
    try:
        upstream_req = client.build_request(
            "POST",
            f"{LLM_SERVICE_URL}/api/chat",
            content=json.dumps({"messages": messages, "stream": body.stream}),
            headers=fwd_headers,
        )
        upstream = await client.send(upstream_req, stream=True)
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM service unreachable: {exc}",
        )

    user_id = user.user_id if user else None
    response_chunks: list[bytes] = []

    async def stream_and_save():
        try:
            async for chunk in upstream.aiter_raw():
                response_chunks.append(chunk)
                yield chunk
        finally:
            await upstream.aclose()

        if user_id is None:
            return
        try:
            full_text = b"".join(response_chunks).decode("utf-8", errors="ignore")
            answer = ""
            # Non-streaming JSON: {"choices":[{"message":{"content": ...}}]}
            try:
                data = json.loads(full_text)
                choices = data.get("choices", [])
                if choices:
                    answer = choices[0].get("message", {}).get("content", "")
            except ValueError:
                pass
            # Streaming SSE: data: {"content": "..."} per line
            if not answer:
                parts = []
                for line in full_text.splitlines():
                    if line.startswith("data:") and "[DONE]" not in line:
                        try:
                            parts.append(json.loads(line[5:].strip()).get("content", ""))
                        except ValueError:
                            pass
                answer = "".join(parts)

            if answer:
                _save_document_chat(
                    user_id=user_id,
                    request_id=request_id,
                    doc=doc,
                    question=body.question,
                    answer=answer,
                    conversation_id=body.conversation_id,
                )
        except Exception as exc:
            logger.error("document_chat_db_save_error: %s", exc)

    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() in ("content-type", "cache-control", "x-accel-buffering")
    }
    return StreamingResponse(
        stream_and_save(),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )
