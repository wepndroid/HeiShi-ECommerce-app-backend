"""Conversation read/unread state (Telegram-style watermark + manual mark)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Conversation, Message, ensure_utc


def is_buyer(conv: Conversation, user_id: str) -> bool:
    return conv.buyer_id == user_id


def counterpart_id(conv: Conversation, user_id: str) -> str:
    return conv.seller_id if is_buyer(conv, user_id) else conv.buyer_id


def marked_as_unread(conv: Conversation, user_id: str) -> bool:
    if is_buyer(conv, user_id):
        return bool(conv.buyer_marked_unread)
    return bool(conv.seller_marked_unread)


def count_unread_incoming(db: Session, conv: Conversation, *, for_buyer: bool) -> int:
    counterpart = conv.seller_id if for_buyer else conv.buyer_id
    watermark = conv.buyer_read_inbox_at if for_buyer else conv.seller_read_inbox_at
    q = db.query(Message).filter(
        Message.conversation_id == conv.id,
        Message.sender_id == counterpart,
    )
    if watermark is not None:
        q = q.filter(Message.sent_at > ensure_utc(watermark))
    return q.count()


def bump_unread_for_recipient(db: Session, conv: Conversation, sender_id: str) -> None:
    if conv.buyer_id == sender_id:
        conv.seller_unread = count_unread_incoming(db, conv, for_buyer=False)
    else:
        conv.buyer_unread = count_unread_incoming(db, conv, for_buyer=True)


def mark_conversation_read(
    db: Session,
    conv: Conversation,
    user_id: str,
    max_message_id: str | None = None,
) -> None:
    cp_id = counterpart_id(conv, user_id)
    read_up_to: datetime | None = None

    if max_message_id:
        msg = (
            db.query(Message)
            .filter(Message.id == max_message_id, Message.conversation_id == conv.id)
            .first()
        )
        if msg:
            if msg.sender_id == cp_id:
                read_up_to = msg.sent_at
            elif msg.sender_id == user_id:
                read_up_to = (
                    db.query(func.max(Message.sent_at))
                    .filter(
                        Message.conversation_id == conv.id,
                        Message.sender_id == cp_id,
                        Message.sent_at <= msg.sent_at,
                    )
                    .scalar()
                )

    if read_up_to is None:
        latest = (
            db.query(Message)
            .filter(Message.conversation_id == conv.id, Message.sender_id == cp_id)
            .order_by(Message.sent_at.desc())
            .first()
        )
        read_up_to = latest.sent_at if latest else None

    if is_buyer(conv, user_id):
        conv.buyer_marked_unread = False
        if read_up_to is not None:
            read_up_to = ensure_utc(read_up_to)
            current = conv.buyer_read_inbox_at
            if current is None or read_up_to >= ensure_utc(current):
                conv.buyer_read_inbox_at = read_up_to
        conv.buyer_unread = count_unread_incoming(db, conv, for_buyer=True)
    else:
        conv.seller_marked_unread = False
        if read_up_to is not None:
            read_up_to = ensure_utc(read_up_to)
            current = conv.seller_read_inbox_at
            if current is None or read_up_to >= ensure_utc(current):
                conv.seller_read_inbox_at = read_up_to
        conv.seller_unread = count_unread_incoming(db, conv, for_buyer=False)


def set_marked_as_unread(conv: Conversation, user_id: str, marked: bool) -> None:
    if is_buyer(conv, user_id):
        conv.buyer_marked_unread = marked
    else:
        conv.seller_marked_unread = marked


def message_ack_read(conv: Conversation, msg: Message, viewer_id: str) -> bool:
    """True when the recipient has read an outgoing message sent by the viewer."""
    if msg.sender_id != viewer_id:
        return False
    watermark = conv.seller_read_inbox_at if conv.buyer_id == viewer_id else conv.buyer_read_inbox_at
    if watermark is None:
        return False
    return ensure_utc(msg.sent_at) <= ensure_utc(watermark)


def backfill_read_watermarks(db: Session) -> None:
    """One-time dev migration: treat zero-unread conversations as fully read."""
    convs = db.query(Conversation).all()
    for conv in convs:
        changed = False
        if conv.buyer_unread == 0 and conv.buyer_read_inbox_at is None and conv.last_message_at:
            conv.buyer_read_inbox_at = conv.last_message_at
            changed = True
        if conv.seller_unread == 0 and conv.seller_read_inbox_at is None and conv.last_message_at:
            conv.seller_read_inbox_at = conv.last_message_at
            changed = True
        if changed:
            db.add(conv)
    db.commit()
