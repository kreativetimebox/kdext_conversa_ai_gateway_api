"""Chat history endpoints — persist conversations + messages in the gateway DB.

Mounted at root-level /conversations (NOT under /api) so it isn't captured by
the LLM-service proxy's /api/{path} catch-all. Every route is scoped to the
authenticated API-key user, so users only ever see their own chats.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from app.core.dependencies import verify_api_key
from app.database import get_db
from app.models.conversation import ChatMessage, Conversation
from app.models.user import User
from app.schemas.conversation import (
    ConversationCreate,
    ConversationDetail,
    ConversationOut,
    MessageCreate,
    MessageOut,
)

router = APIRouter(prefix="/conversations", tags=["history"])


def _owned_conversation(conversation_id: int, user: User, db: Session) -> Conversation:
    conv = (
        db.query(Conversation)
        .filter(
            Conversation.conversation_id == conversation_id,
            Conversation.user_id == user.user_id,
        )
        .first()
    )
    if not conv:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        )
    return conv


@router.post("", response_model=ConversationDetail, status_code=status.HTTP_201_CREATED)
def create_conversation(
    body: ConversationCreate,
    user: User = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Create a conversation, optionally seeded with its first messages."""
    conv = Conversation(user_id=user.user_id, title=body.title, mode=body.mode)
    db.add(conv)
    db.flush()  # assign conversation_id before adding messages

    for m in body.messages:
        db.add(ChatMessage(
            conversation_id=conv.conversation_id,
            user_id=user.user_id,
            role=m.role,
            content=m.content,
            source_lang=m.source_lang,
            target_lang=m.target_lang,
            engine=m.engine,
        ))

    db.commit()
    db.refresh(conv)
    return conv


@router.get("", response_model=list[ConversationOut])
def list_conversations(
    user: User = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """List the user's conversations, most recently updated first."""
    return (
        db.query(Conversation)
        .filter(Conversation.user_id == user.user_id)
        .order_by(Conversation.updated_at.desc())
        .all()
    )


@router.get("/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: int,
    user: User = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Fetch a conversation with all its messages."""
    return _owned_conversation(conversation_id, user, db)


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
)
def add_message(
    conversation_id: int,
    body: MessageCreate,
    user: User = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Append a message to a conversation."""
    conv = _owned_conversation(conversation_id, user, db)
    msg = ChatMessage(
        conversation_id=conv.conversation_id,
        user_id=user.user_id,
        role=body.role,
        content=body.content,
        source_lang=body.source_lang,
        target_lang=body.target_lang,
        engine=body.engine,
    )
    db.add(msg)
    # Touch the conversation so list ordering reflects the latest activity
    # (DB-side clock — works on Postgres and SQLite).
    conv.updated_at = func.now()
    db.commit()
    db.refresh(msg)
    return msg


@router.patch("/{conversation_id}", response_model=ConversationOut)
def rename_conversation(
    conversation_id: int,
    body: ConversationCreate,
    user: User = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Update a conversation's title/mode (messages in the body are ignored)."""
    conv = _owned_conversation(conversation_id, user, db)
    conv.title = body.title
    conv.mode = body.mode
    db.commit()
    db.refresh(conv)
    return conv


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(
    conversation_id: int,
    user: User = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Delete a conversation and all its messages."""
    conv = _owned_conversation(conversation_id, user, db)
    db.delete(conv)
    db.commit()
