"""Conversation + ChatMessage ORM models — chat/translation history storage.

These persist the chatbot's conversations server-side (instead of only in the
browser), so a user's chats and translations are saved in the gateway DB and
retrievable across devices. One Conversation has many ChatMessages.
"""

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, ForeignKey, Index,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class Conversation(Base):
    __tablename__ = "conversations"

    conversation_id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title = Column(String(255), nullable=False, default="New Chat")
    mode = Column(String(20), nullable=False, default="chat")  # 'chat' | 'translate'
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    messages = relationship(
        "ChatMessage",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ChatMessage.message_id",
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    message_id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(
        Integer,
        ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        Integer,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = Column(String(20), nullable=False)  # 'user' | 'assistant' | 'system'
    content = Column(Text, nullable=False)
    # Translation metadata (null for plain chat turns).
    source_lang = Column(String(10), nullable=True)
    target_lang = Column(String(10), nullable=True)
    engine = Column(String(20), nullable=True)  # 'llm' | 'api' | model name
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    conversation = relationship("Conversation", back_populates="messages")


Index("ix_chat_messages_conv_msg", ChatMessage.conversation_id, ChatMessage.message_id)
