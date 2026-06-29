"""Pydantic schemas for conversation/chat-history endpoints."""

from datetime import datetime

from pydantic import BaseModel, Field


class MessageCreate(BaseModel):
    role: str = Field(..., pattern="^(user|assistant|system)$")
    content: str = Field(..., min_length=1)
    source_lang: str | None = Field(default=None, max_length=10)
    target_lang: str | None = Field(default=None, max_length=10)
    engine: str | None = Field(default=None, max_length=20)


class ConversationCreate(BaseModel):
    title: str = Field(default="New Chat", max_length=255)
    mode: str = Field(default="chat", pattern="^(chat|translate)$")
    # Optionally seed the conversation with its first messages in one call.
    messages: list[MessageCreate] = Field(default_factory=list)


class MessageOut(BaseModel):
    message_id: int
    role: str
    content: str
    source_lang: str | None = None
    target_lang: str | None = None
    engine: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ConversationOut(BaseModel):
    conversation_id: int
    title: str
    mode: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ConversationDetail(ConversationOut):
    messages: list[MessageOut] = Field(default_factory=list)
