"""Inbox conversation deduplication and lookup helpers."""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy.orm import Session

from app.models import Conversation, Message


def counterpart_for_user(conv: Conversation, user_id: str) -> str:
    return conv.seller_id if conv.buyer_id == user_id else conv.buyer_id


def _activity_key(conv: Conversation) -> tuple:
    return (
        conv.last_message_at or conv.created_at,
        conv.created_at,
    )


def filter_inbox_conversations(conversations: list[Conversation], user_id: str) -> list[Conversation]:
    """Hide empty threads when the same counterpart already has an active conversation."""
    by_counterpart: dict[str, list[Conversation]] = defaultdict(list)
    for conv in conversations:
        by_counterpart[counterpart_for_user(conv, user_id)].append(conv)

    visible: list[Conversation] = []
    for group in by_counterpart.values():
        with_messages = [c for c in group if c.last_message_text]
        if with_messages:
            visible.extend(with_messages)
            continue
        newest = max(group, key=_activity_key)
        visible.append(newest)

    visible.sort(key=_activity_key, reverse=True)
    return visible


def find_conversation_for_open(
    db: Session,
    *,
    listing_id: int,
    buyer_id: str,
    seller_id: str,
) -> Conversation | None:
    exact = (
        db.query(Conversation)
        .filter(
            Conversation.listing_id == listing_id,
            Conversation.buyer_id == buyer_id,
            Conversation.seller_id == seller_id,
        )
        .first()
    )
    if exact:
        return exact
    return None


def cleanup_duplicate_empty_conversations(db: Session) -> None:
    """Remove redundant empty threads for the same buyer/seller pair."""
    groups: dict[tuple[str, str], list[Conversation]] = defaultdict(list)
    for conv in db.query(Conversation).all():
        groups[(conv.buyer_id, conv.seller_id)].append(conv)

    changed = False
    for group in groups.values():
        if len(group) < 2:
            continue
        with_messages = [c for c in group if c.last_message_text]
        if with_messages:
            for conv in group:
                if conv.last_message_text:
                    continue
                db.query(Message).filter(Message.conversation_id == conv.id).delete()
                db.delete(conv)
                changed = True
            continue
        newest = max(group, key=_activity_key)
        for conv in group:
            if conv.id == newest.id:
                continue
            db.query(Message).filter(Message.conversation_id == conv.id).delete()
            db.delete(conv)
            changed = True

    if changed:
        db.commit()