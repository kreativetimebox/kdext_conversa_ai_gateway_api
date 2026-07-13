"""Pydantic schemas for the document-chat feature (/documents/*)."""

from pydantic import BaseModel, Field


class DocumentReferenceIn(BaseModel):
    """Reference a scanned document by its OCR request id."""

    request_id: str = Field(min_length=1, max_length=128)
    # Force a re-fetch from the OCR service even if the document is cached
    # (e.g. the OCR job finished after the first attempt).
    refresh: bool = False


class DocumentOut(BaseModel):
    """Summary of a referenced document (no full text — use GET /documents/{id})."""

    request_id: str
    status: str  # OCR-side status if reported, else 'completed'
    filename: str | None = None
    pages: int | None = None
    characters: int
    preview: str  # first few hundred chars of the scanned text
    cached: bool  # True when served from the gateway's in-process cache


class DocumentContentOut(DocumentOut):
    """Full scanned content of a referenced document."""

    text: str


class DocumentChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str


class DocumentChatIn(BaseModel):
    """Ask a question about a referenced document."""

    question: str = Field(min_length=1)
    stream: bool = True
    # Continue an existing gateway conversation (ownership verified at save time).
    conversation_id: int | None = None
    # Optional prior turns so the LLM keeps multi-question context. The document
    # itself is always injected server-side — clients never resend it.
    history: list[DocumentChatMessage] = []
