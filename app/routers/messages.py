from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session, joinedload

from app.auth import get_accept_language, get_current_user
from app.database import get_db
from app.models import Conversation, Listing, Message, SystemNotification, User
from app.pagination import paginate
from app.schemas import (
    ChatMessageDto,
    ConversationDto,
    InboxNotificationDto,
    NotificationGroupDto,
    OpenConversationRequest,
    Paginated,
    SendMessageRequest,
    SystemNotificationDto,
)
from app.serializers import (
    conversation_to_dto,
    inbox_notification_to_dto,
    iso,
    message_to_dto,
    notification_group_to_dto,
    system_notification_to_dto,
)

router = APIRouter(tags=["messaging"])
notifications_router = APIRouter(prefix="/notifications", tags=["notifications"])

NOTIFICATION_CATEGORIES = ("system", "order", "follow")


@router.get("/conversations", response_model=Paginated[ConversationDto])
def list_conversations(
    request: Request,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lang = get_accept_language(request)
    q = (
        db.query(Conversation)
        .options(joinedload(Conversation.listing), joinedload(Conversation.buyer), joinedload(Conversation.seller))
        .filter((Conversation.buyer_id == user.id) | (Conversation.seller_id == user.id))
        .order_by(Conversation.last_message_at.desc().nullslast(), Conversation.created_at.desc())
    )
    total = q.count()
    convs = q.offset((page - 1) * pageSize).limit(pageSize).all()
    return paginate([conversation_to_dto(c, user.id, lang) for c in convs], page, pageSize, total)


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
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lang = get_accept_language(request)
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
        return conversation_to_dto(existing, user.id, lang)
    conv = Conversation(listing_id=body.listingId, buyer_id=user.id, seller_id=seller_id)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    conv.listing = listing
    conv.buyer = user
    conv.seller = listing.seller
    return conversation_to_dto(conv, user.id, lang)


@notifications_router.get("/groups", response_model=list[NotificationGroupDto])
def list_notification_groups(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lang = get_accept_language(request)
    groups: list[NotificationGroupDto] = []
    for category in NOTIFICATION_CATEGORIES:
        base = db.query(SystemNotification).filter(
            SystemNotification.user_id == user.id,
            SystemNotification.category == category,
        )
        unread_count = base.filter(SystemNotification.unread.is_(True)).count()
        latest = base.order_by(SystemNotification.created_at.desc()).first()
        groups.append(notification_group_to_dto(category, unread_count, latest, lang))
    return groups


@notifications_router.get("/groups/{category}", response_model=Paginated[InboxNotificationDto])
def list_group_notifications(
    category: str,
    request: Request,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if category not in NOTIFICATION_CATEGORIES:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Unknown notification group", "details": {}})
    lang = get_accept_language(request)
    q = (
        db.query(SystemNotification)
        .filter(SystemNotification.user_id == user.id, SystemNotification.category == category)
        .order_by(SystemNotification.created_at.desc())
    )
    total = q.count()
    items = q.offset((page - 1) * pageSize).limit(pageSize).all()
    return paginate([inbox_notification_to_dto(n, lang) for n in items], page, pageSize, total)


@notifications_router.post("/groups/{category}/mark-read", status_code=204)
def mark_group_read(
    category: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if category not in NOTIFICATION_CATEGORIES:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Unknown notification group", "details": {}})
    (
        db.query(SystemNotification)
        .filter(
            SystemNotification.user_id == user.id,
            SystemNotification.category == category,
            SystemNotification.unread.is_(True),
        )
        .update({SystemNotification.unread: False})
    )
    db.commit()


@notifications_router.delete("/{notification_id}", status_code=204)
def delete_notification(
    notification_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = (
        db.query(SystemNotification)
        .filter(SystemNotification.id == notification_id, SystemNotification.user_id == user.id)
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Notification not found", "details": {}})
    db.delete(item)
    db.commit()


@notifications_router.get("/system", response_model=Paginated[SystemNotificationDto])
def list_system_notifications(
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = (
        db.query(SystemNotification)
        .filter(SystemNotification.user_id == user.id, SystemNotification.category == "system")
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
