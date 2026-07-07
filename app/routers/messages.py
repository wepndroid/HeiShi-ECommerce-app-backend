from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, BackgroundTasks
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload

from app.auth import get_accept_language, get_current_user
from app.blocklist_helpers import users_blocked
from app.database import SessionLocal, get_db
from app.push_notifications import send_chat_message_push
from app.catalog_helpers import get_or_create_settings
from app.conversation_inbox import (
    cleanup_duplicate_empty_conversations,
    filter_inbox_conversations,
    find_conversation_for_open,
)
from app.messaging_read import (
    bump_unread_for_recipient,
    mark_conversation_read,
    set_marked_as_unread,
)
from app.models import Conversation, Listing, Message, Order, SystemNotification, User
from app.pagination import paginate
from app.schemas import (
    ChatMessageDto,
    ConversationDto,
    InboxNotificationDto,
    MarkConversationReadRequest,
    MarkConversationUnreadRequest,
    NotificationGroupDto,
    OpenConversationRequest,
    Paginated,
    SendMessageRequest,
    SystemNotificationDto,
)
from app.serializers import (
    conversation_to_dto,
    inbox_notification_to_dto,
    message_to_dto,
    notification_group_to_dto,
    system_notification_to_dto,
)

router = APIRouter(tags=["messaging"])
notifications_router = APIRouter(prefix="/notifications", tags=["notifications"])

NOTIFICATION_CATEGORIES = ("system", "order", "follow")

_CATEGORY_SETTING_ATTR = {
    "system": "intent_alerts",
    "order": "review_results",
    "follow": "marketing",
}


def _notification_category_enabled(settings, category: str) -> bool:
    attr = _CATEGORY_SETTING_ATTR.get(category)
    if not attr:
        return True
    return bool(getattr(settings, attr, True))


def _dispatch_chat_push(
    recipient_id: str,
    sender_name: str,
    message_preview: str,
    conversation_id: str,
) -> None:
    db = SessionLocal()
    try:
        recipient = db.query(User).filter(User.id == recipient_id).first()
        lang = recipient.language if recipient and recipient.language else "en"
        send_chat_message_push(
            db,
            recipient_id=recipient_id,
            sender_name=sender_name,
            message_preview=message_preview,
            conversation_id=conversation_id,
            lang=lang,
        )
    finally:
        db.close()


OPEN_ORDER_STATUSES = ("pendingPay", "pendingShip", "pendingReceive", "pendingReview", "completed")


def _conversation_counterpart_id(conv: Conversation, user_id: str) -> str:
    return conv.seller_id if user_id == conv.buyer_id else conv.buyer_id


def _ensure_not_blocked(db: Session, user_id: str, other_id: str) -> None:
    if users_blocked(db, user_id, other_id):
        raise HTTPException(
            status_code=403,
            detail={"code": "USER_BLOCKED", "message": "You cannot message this user", "details": {}},
        )


def _participants_have_listing_order(
    db: Session,
    listing_id: int,
    buyer_id: str,
    seller_id: str,
) -> bool:
    return (
        db.query(Order.id)
        .filter(
            Order.listing_id == listing_id,
            Order.buyer_id == buyer_id,
            Order.seller_id == seller_id,
            Order.status.in_(OPEN_ORDER_STATUSES),
        )
        .first()
        is not None
    )


def _listing_chat_unavailable(listing: Listing, db: Session, buyer_id: str, seller_id: str) -> bool:
    if listing.status == "active":
        return False
    return not _participants_have_listing_order(db, listing.id, buyer_id, seller_id)


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
    convs = q.all()
    visible = filter_inbox_conversations(convs, user.id)
    visible = [c for c in visible if not users_blocked(db, user.id, _conversation_counterpart_id(c, user.id))]
    total = len(visible)
    start = (page - 1) * pageSize
    page_convs = visible[start : start + pageSize]
    return paginate([conversation_to_dto(c, user.id, lang) for c in page_convs], page, pageSize, total)


@router.get("/conversations/{conversation_id}", response_model=ConversationDto)
def get_conversation(
    conversation_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lang = get_accept_language(request)
    conv = _get_conversation(db, conversation_id, user.id)
    return conversation_to_dto(conv, user.id, lang)


@router.get("/conversations/{conversation_id}/messages", response_model=Paginated[ChatMessageDto])
def get_messages(
    conversation_id: str,
    before: str | None = None,
    beforeId: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = _get_conversation(db, conversation_id, user.id)
    db.refresh(conv)
    q = db.query(Message).filter(Message.conversation_id == conv.id)
    if beforeId:
        cursor = (
            db.query(Message)
            .filter(Message.conversation_id == conv.id, Message.id == beforeId)
            .first()
        )
        if not cursor:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_CURSOR", "message": "Invalid message cursor", "details": {}},
            )
        q = q.filter(
            or_(
                Message.sent_at < cursor.sent_at,
                and_(Message.sent_at == cursor.sent_at, Message.id < beforeId),
            )
        )
    elif before:
        try:
            before_dt = datetime.fromisoformat(before.replace("Z", "+00:00"))
            q = q.filter(Message.sent_at < before_dt)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_CURSOR", "message": "Invalid before timestamp", "details": {}},
            )
    q = q.order_by(Message.sent_at.desc(), Message.id.desc())
    total = q.count()
    msgs = q.limit(limit).all()
    return paginate([message_to_dto(m, conv, user.id) for m in msgs], 1, limit, total)


@router.post("/conversations/{conversation_id}/read", response_model=ConversationDto)
def mark_conversation_read_endpoint(
    conversation_id: str,
    body: MarkConversationReadRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lang = get_accept_language(request)
    conv = _get_conversation(db, conversation_id, user.id)
    mark_conversation_read(db, conv, user.id, body.maxMessageId)
    db.commit()
    db.refresh(conv)
    return conversation_to_dto(conv, user.id, lang)


@router.patch("/conversations/{conversation_id}/read-state", response_model=ConversationDto)
def update_conversation_read_state(
    conversation_id: str,
    body: MarkConversationUnreadRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lang = get_accept_language(request)
    conv = _get_conversation(db, conversation_id, user.id)
    set_marked_as_unread(conv, user.id, body.markedAsUnread)
    db.commit()
    db.refresh(conv)
    return conversation_to_dto(conv, user.id, lang)


@router.post("/conversations/{conversation_id}/messages", response_model=ChatMessageDto, status_code=201)
def send_message(
    conversation_id: str,
    body: SendMessageRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = _get_conversation(db, conversation_id, user.id)
    # Admin mute (禁言): a muted account cannot send chat messages.
    if getattr(user, "is_muted", False):
        raise HTTPException(
            status_code=403,
            detail={"code": "USER_MUTED", "message": "Your account is muted and cannot send messages", "details": {}},
        )
    listing = db.query(Listing).filter(Listing.id == conv.listing_id).first()
    if listing and _listing_chat_unavailable(listing, db, conv.buyer_id, conv.seller_id):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_STATE", "message": "Listing is not available for chat", "details": {}},
        )
    text = body.text.strip()
    if not text:
        raise HTTPException(
            status_code=422,
            detail={"code": "VALIDATION_ERROR", "message": "Message text is required", "details": {}},
        )
    msg = Message(conversation_id=conv.id, sender_id=user.id, text=text)
    now = datetime.now(timezone.utc)
    conv.last_message_text = msg.text
    conv.last_message_at = now
    db.add(msg)
    db.flush()
    bump_unread_for_recipient(db, conv, user.id)
    db.commit()
    db.refresh(msg)

    recipient_id = conv.seller_id if user.id == conv.buyer_id else conv.buyer_id
    background_tasks.add_task(
        _dispatch_chat_push,
        recipient_id,
        user.nickname,
        text,
        conv.id,
    )

    return message_to_dto(msg, conv, user.id)


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
    if user.id == listing.seller_id:
        if not body.counterpartUserId:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "VALIDATION_ERROR",
                    "message": "counterpartUserId is required when opening chat as the listing seller",
                    "details": {},
                },
            )
        buyer_id = body.counterpartUserId
        seller_id = user.id
    else:
        buyer_id = user.id
        seller_id = listing.seller_id

    if buyer_id == seller_id:
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATE", "message": "Cannot chat with yourself", "details": {}})

    counterpart_id = buyer_id if user.id == seller_id else seller_id
    _ensure_not_blocked(db, user.id, counterpart_id)
    existing = find_conversation_for_open(
        db,
        listing_id=body.listingId,
        buyer_id=buyer_id,
        seller_id=seller_id,
    )
    if existing:
        conv = _get_conversation(db, existing.id, user.id)
        return conversation_to_dto(conv, user.id, lang)
    if _listing_chat_unavailable(listing, db, buyer_id, seller_id):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_STATE", "message": "Listing is not available for chat", "details": {}},
        )
    conv = Conversation(listing_id=body.listingId, buyer_id=buyer_id, seller_id=seller_id)
    db.add(conv)
    db.commit()
    conv = _get_conversation(db, conv.id, user.id)
    return conversation_to_dto(conv, user.id, lang)


@notifications_router.get("/groups", response_model=list[NotificationGroupDto])
def list_notification_groups(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lang = get_accept_language(request)
    settings = get_or_create_settings(db, user.id)
    groups: list[NotificationGroupDto] = []
    for category in NOTIFICATION_CATEGORIES:
        if not _notification_category_enabled(settings, category):
            groups.append(notification_group_to_dto(category, 0, None, lang))
            continue
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
    settings = get_or_create_settings(db, user.id)
    if not _notification_category_enabled(settings, category):
        return paginate([], page, pageSize, 0)
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
    if users_blocked(db, user_id, _conversation_counterpart_id(conv, user_id)):
        raise HTTPException(
            status_code=403,
            detail={"code": "USER_BLOCKED", "message": "You cannot message this user", "details": {}},
        )
    return conv
