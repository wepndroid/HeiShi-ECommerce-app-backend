from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user
from app.database import get_db
from app.models import Conversation, Listing, Message, SystemNotification, User
from app.pagination import paginate
from app.schemas import (
    ChatMessageDto,
    ConversationDto,
    OpenConversationRequest,
    Paginated,
    SendMessageRequest,
    SystemNotificationDto,
)
from app.serializers import conversation_to_dto, iso, message_to_dto, system_notification_to_dto

router = APIRouter(tags=["messaging"])
notifications_router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/conversations", response_model=Paginated[ConversationDto])
def list_conversations(
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = (
        db.query(Conversation)
        .options(joinedload(Conversation.listing), joinedload(Conversation.buyer), joinedload(Conversation.seller))
        .filter((Conversation.buyer_id == user.id) | (Conversation.seller_id == user.id))
        .order_by(Conversation.last_message_at.desc().nullslast(), Conversation.created_at.desc())
    )
    total = q.count()
    convs = q.offset((page - 1) * pageSize).limit(pageSize).all()
    return paginate([conversation_to_dto(c, user.id) for c in convs], page, pageSize, total)


@router.get("/conversations/{conversation_id}/messages", response_model=Paginated[ChatMessageDto])
def get_messages(
    conversation_id: str,
    before: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = _get_conversation(db, conversation_id, user.id)
    q = db.query(Message).filter(Message.conversation_id == conv.id)
    if before:
        try:
            before_dt = datetime.fromisoformat(before.replace("Z", "+00:00"))
            q = q.filter(Message.sent_at < before_dt)
        except ValueError:
            pass
    q = q.order_by(Message.sent_at.desc())
    total = q.count()
    msgs = q.limit(limit).all()
    is_buyer = conv.buyer_id == user.id
    if is_buyer:
        conv.buyer_unread = 0
    else:
        conv.seller_unread = 0
    db.commit()
    return paginate([message_to_dto(m) for m in msgs], 1, limit, total)


@router.post("/conversations/{conversation_id}/messages", response_model=ChatMessageDto, status_code=201)
def send_message(
    conversation_id: str,
    body: SendMessageRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = _get_conversation(db, conversation_id, user.id)
    msg = Message(conversation_id=conv.id, sender_id=user.id, text=body.text.strip())
    now = datetime.now(timezone.utc)
    conv.last_message_text = msg.text
    conv.last_message_at = now
    if conv.buyer_id == user.id:
        conv.seller_unread += 1
    else:
        conv.buyer_unread += 1
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return message_to_dto(msg)


@router.post("/conversations", response_model=ConversationDto, status_code=201)
def open_conversation(
    body: OpenConversationRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    listing = db.query(Listing).options(joinedload(Listing.seller)).filter(Listing.id == body.listingId).first()
    if not listing:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Listing not found", "details": {}})
    seller_id = body.counterpartUserId or listing.seller_id
    if seller_id == user.id:
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATE", "message": "Cannot chat with yourself", "details": {}})
    buyer_id = user.id if user.id != seller_id else user.id
    if user.id == seller_id:
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATE", "message": "Buyer required", "details": {}})
    existing = (
        db.query(Conversation)
        .options(joinedload(Conversation.listing), joinedload(Conversation.buyer), joinedload(Conversation.seller))
        .filter(Conversation.listing_id == body.listingId, Conversation.buyer_id == user.id, Conversation.seller_id == seller_id)
        .first()
    )
    if existing:
        return conversation_to_dto(existing, user.id)
    conv = Conversation(listing_id=body.listingId, buyer_id=user.id, seller_id=seller_id)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    conv.listing = listing
    conv.buyer = user
    conv.seller = listing.seller
    return conversation_to_dto(conv, user.id)


@notifications_router.get("/system", response_model=Paginated[SystemNotificationDto])
def list_system_notifications(
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = (
        db.query(SystemNotification)
        .filter(SystemNotification.user_id == user.id)
        .order_by(SystemNotification.created_at.desc())
    )
    total = q.count()
    items = q.offset((page - 1) * pageSize).limit(pageSize).all()
    return paginate([system_notification_to_dto(n) for n in items], page, pageSize, total)


def _get_conversation(db: Session, conversation_id: str, user_id: str) -> Conversation:
    conv = (
        db.query(Conversation)
        .options(joinedload(Conversation.listing), joinedload(Conversation.buyer), joinedload(Conversation.seller))
        .filter(Conversation.id == conversation_id)
        .first()
    )
    if not conv or user_id not in (conv.buyer_id, conv.seller_id):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Conversation not found", "details": {}})
    return conv
