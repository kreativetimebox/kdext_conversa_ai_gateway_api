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

import asyncio
import json
import logging
import re

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
_FAILED_STATUSES = {"failed", "error", "cancelled"}


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


def extract_document_from_text(raw: str) -> dict:
    """Parse the OCR service's plain-text response format.

    The live API (verified 2026-07-13) returns text/plain in a compact
    "key: value" style with indented nested blocks, e.g.:

        request_id: req_...
        status: completed
        filename: invoice.pdf
        formatted_result:
          supplierName: Perth Garage Ltd
          totalAmount: 150.82
          ...
        document_url: "https://...presigned..."

    The whole body IS the scanned content in LLM-readable form, so it is used
    verbatim as the document text — minus the presigned document_url line,
    which is token noise and a signed link the model has no business seeing.
    """
    def _field(name: str) -> str | None:
        m = re.search(rf"^{name}:[ \t]*(.+)$", raw, re.MULTILINE)
        if not m:
            return None
        value = m.group(1).strip().strip('"')
        return value if value and value.lower() not in ("null", "unknown", "none") else None

    text_lines = [
        line for line in raw.splitlines()
        if not line.lstrip().startswith("document_url:")
    ]
    return {
        "text": "\n".join(text_lines).strip(),
        "status": (_field("status") or "completed").lower(),
        "filename": _field("filename"),
        "pages": None,
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

    # The live OCR API answers in a compact plain-text "key: value" format
    # (content-type text/plain); a JSON body is handled too in case the
    # service changes. Try JSON first, fall back to the text parser.
    try:
        doc = extract_document(resp.json())
    except ValueError:
        doc = extract_document_from_text(resp.text)

    if doc["status"] in _PENDING_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Document is not ready yet (status: {doc['status']}). "
                   "Retry once the OCR scan completes.",
        )
    if doc["status"] in _FAILED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"OCR scan failed for this document (status: {doc['status']})",
        )
    if not doc["text"]:
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


# ── Context-window budgeting ──────────────────────────────────────────────────
# The deployed engine hard-rejects prompt + max_tokens > llm_context_tokens
# ("This model's maximum context length is 8192 tokens..."), so everything
# sent must be budgeted. OCR text is number/punctuation-heavy and tokenizes
# poorly — 3 chars/token is the safe estimate (4 underestimates real usage).
_CHARS_PER_TOKEN = 3
_MIN_OUTPUT_TOKENS = 512    # always leave room for a useful answer
_OVERHEAD_TOKENS = 300      # LLM-service system prompt + chat template + instructions
_MAX_HISTORY_TOKENS = 1500  # prior turns beyond this are dropped (oldest first)
# Documents too big for one request are map-reduced: each chunk is scanned for
# question-relevant info in its own LLM call, then one final call synthesizes
# the streamed answer — so the WHOLE document is read, at any size (up to the
# chunk cap, which bounds latency).
_MAX_CHUNKS = 6
_CHUNK_ANSWER_TOKENS = 400  # per-chunk extraction output budget


def _estimate_tokens(text: str) -> int:
    return len(text or "") // _CHARS_PER_TOKEN + 1


def _budget_history(history: list[dict]) -> list[dict]:
    """Keep the most recent prior turns that fit the history token budget."""
    kept: list[dict] = []
    used = 0
    for msg in reversed(history):
        cost = _estimate_tokens(msg["content"])
        if used + cost > _MAX_HISTORY_TOKENS:
            break
        kept.append(msg)
        used += cost
    return list(reversed(kept))


def _document_question_prompt(
    request_id: str, doc: dict, question: str, max_doc_chars: int
) -> str:
    """Document + instructions + question as ONE user message.

    Deliberately NOT a system-role message: the deployed LLM service streams
    zero tokens when the conversation starts with a system turn (verified
    2026-07-13 — the exact same request with user/assistant roles only, the
    shape the main chat UI always sends, works). So the document context is
    folded into the user turn instead.
    """
    text = doc["text"][: min(max_doc_chars, settings.max_document_context_chars)]
    truncated = len(text) < len(doc["text"])
    name = doc["filename"] or request_id
    return (
        f"I have attached a scanned document named '{name}'. Answer my question "
        "using ONLY the document content below — everything the OCR scan "
        "extracted from it. If the answer is not present in the document, say "
        "so plainly instead of guessing. Quote the document where it helps."
        + (
            " (Note: the document was truncated to fit the model's context limit.)"
            if truncated else ""
        )
        + "\n\n"
        "--- DOCUMENT CONTENT START ---\n"
        f"{text}\n"
        "--- DOCUMENT CONTENT END ---\n\n"
        f"My question: {question}"
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


async def _complete_chat_once(messages: list[dict], max_tokens: int) -> str:
    """One non-streaming /api/chat call; returns the assistant text ('' on failure)."""
    headers = {"Content-Type": "application/json"}
    if LLM_SERVICE_API_KEY:
        headers["X-Service-Key"] = LLM_SERVICE_API_KEY
    client = _get_client()
    try:
        resp = await client.post(
            f"{LLM_SERVICE_URL}/api/chat",
            content=json.dumps({
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": max_tokens,
                "stream": False,
            }),
            headers=headers,
        )
        if resp.status_code != 200:
            logger.warning("chunk scan: LLM returned %s: %s", resp.status_code, resp.text[:200])
            return ""
        data = resp.json()
        choices = data.get("choices", [])
        return choices[0].get("message", {}).get("content", "") if choices else ""
    except Exception as exc:
        logger.warning("chunk scan call failed: %s", exc)
        return ""


async def _scan_document_chunks(doc: dict, question: str, name: str) -> list[str]:
    """Map step: run each document chunk through the LLM to extract everything
    relevant to the question. The whole document is read regardless of size
    (up to _MAX_CHUNKS × chunk size — beyond that the tail is dropped and the
    synthesis prompt says so)."""
    chunk_budget_tokens = (
        settings.llm_context_tokens
        - _OVERHEAD_TOKENS
        - _CHUNK_ANSWER_TOKENS
        - _estimate_tokens(question)
        - 150  # per-chunk instructions
    )
    chunk_chars = max(chunk_budget_tokens * _CHARS_PER_TOKEN, 1000)
    text = doc["text"]
    chunks = [text[i: i + chunk_chars] for i in range(0, len(text), chunk_chars)]
    dropped = max(0, len(chunks) - _MAX_CHUNKS)
    chunks = chunks[:_MAX_CHUNKS]
    logger.info(
        "document map-reduce: %d chars → %d chunks (%d dropped)",
        len(text), len(chunks), dropped,
    )

    # Two at a time — enough parallelism to cut latency without flooding the GPU.
    sem = asyncio.Semaphore(2)

    async def scan(idx: int, chunk: str) -> str:
        prompt = (
            f"Below is part {idx + 1} of {len(chunks)} of a scanned document "
            f"named '{name}'. Extract every piece of information from it that "
            f"is relevant to this question: \"{question}\". Quote exact values "
            "and numbers. If nothing in this part is relevant, reply with "
            "exactly: NONE\n\n"
            f"{chunk}"
        )
        async with sem:
            return await _complete_chat_once(
                [{"role": "user", "content": prompt}], _CHUNK_ANSWER_TOKENS
            )

    results = await asyncio.gather(*(scan(i, c) for i, c in enumerate(chunks)))
    notes = [
        r.strip() for r in results
        if r and r.strip() and r.strip().upper() not in ("NONE", "NONE.")
    ]
    if dropped:
        notes.append(
            f"(Note: the document was longer than {_MAX_CHUNKS} parts; "
            f"{dropped} trailing part(s) could not be scanned.)"
        )
    return notes


def _synthesis_prompt(name: str, question: str, notes: list[str]) -> str:
    """Reduce step: combine per-chunk extracts into the final answer prompt."""
    joined = "\n\n".join(f"[Extract {i + 1}]\n{n}" for i, n in enumerate(notes))
    # Guard the synthesis prompt itself against overflow.
    max_notes_chars = (
        settings.llm_context_tokens - _OVERHEAD_TOKENS - _MIN_OUTPUT_TOKENS - 200
    ) * _CHARS_PER_TOKEN
    joined = joined[:max_notes_chars]
    return (
        f"I asked this question about a scanned document named '{name}': "
        f"\"{question}\"\n\n"
        "The document was scanned in parts; below is every relevant extract "
        "found. Answer my question using ONLY these extracts. If they don't "
        "contain the answer, say so plainly instead of guessing.\n\n"
        f"{joined}"
    )


@router.post("/{request_id}/chat")
async def chat_with_document(
    request_id: str,
    body: DocumentChatIn,
    user=Depends(require_llm_access),
    db: Session = Depends(get_db),
):
    """Answer a question from the scanned document via the LLM service.

    The document text is injected server-side into the final user turn (see
    _document_question_prompt for why not a system message); the client only
    ever sends the question (plus optional prior turns). The response is
    the LLM service's own /api/chat shape — SSE stream when body.stream is
    true, JSON otherwise — so the UI can reuse its existing chat handling.
    """
    _enforce_rate_limit(user, "llm", db)

    doc, _ = await _fetch_document(request_id)

    # Fit everything inside the engine's context window: recent history +
    # instructions + document + question + answer room.
    history = _budget_history([m.model_dump() for m in body.history])
    history_tokens = sum(_estimate_tokens(m["content"]) for m in history)
    available_doc_tokens = (
        settings.llm_context_tokens
        - history_tokens
        - _estimate_tokens(body.question)
        - _OVERHEAD_TOKENS
        - _MIN_OUTPUT_TOKENS
    )
    name = doc["filename"] or request_id

    if _estimate_tokens(doc["text"]) > available_doc_tokens:
        # Document doesn't fit in one request — map-reduce: scan every chunk
        # for question-relevant info, then answer from the combined extracts.
        # The whole document is read; nothing is silently cut.
        notes = await _scan_document_chunks(doc, body.question, name)
        if not notes:
            notes = ["(No relevant information was found in any part of the document.)"]
        user_content = _synthesis_prompt(name, body.question, notes)
    else:
        max_doc_chars = max(available_doc_tokens * _CHARS_PER_TOKEN, 400)
        user_content = _document_question_prompt(
            request_id, doc, body.question, max_doc_chars
        )

    messages = list(history)
    messages.append({"role": "user", "content": user_content})

    prompt_tokens = sum(_estimate_tokens(m["content"]) for m in messages)
    max_tokens = min(
        1024,
        max(128, settings.llm_context_tokens - prompt_tokens - _OVERHEAD_TOKENS),
    )

    fwd_headers = {"Content-Type": "application/json"}
    if LLM_SERVICE_API_KEY:
        fwd_headers["X-Service-Key"] = LLM_SERVICE_API_KEY

    client = _get_client()
    try:
        # Mirror the exact body shape the chat UI sends to /api/chat (the only
        # request shape verified against the live LLM service).
        upstream_req = client.build_request(
            "POST",
            f"{LLM_SERVICE_URL}/api/chat",
            content=json.dumps({
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": max_tokens,
                "stream": body.stream,
            }),
            headers=fwd_headers,
        )
        upstream = await client.send(upstream_req, stream=True)
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM service unreachable: {exc}",
        )

    user_id = user.user_id if (user and body.persist) else None
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
    # /documents/* is a new path for the reverse proxy in front of the gateway —
    # force SSE-friendly behavior so nginx doesn't buffer the stream into one
    # late burst (the /api/chat location is already tuned; this one isn't).
    resp_headers["X-Accel-Buffering"] = "no"
    resp_headers.setdefault("Cache-Control", "no-cache")
    return StreamingResponse(
        stream_and_save(),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )
