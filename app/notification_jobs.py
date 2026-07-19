"""Idempotent scheduled notifications for transaction and interest events."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import (
    BlocklistEntry,
    Conversation,
    Favorite,
    Follow,
    Listing,
    NotificationDispatch,
    NotificationPreference,
    Order,
    SearchLog,
    SystemNotification,
    User,
    ViewHistory,
    utcnow,
)
from app.push_notifications import send_generic_push


def _preference(
    db: Session,
    *,
    user_id: str,
    role: str,
    category: str,
    mandatory: bool,
) -> tuple[bool, bool]:
    row = (
        db.query(NotificationPreference)
        .filter(
            NotificationPreference.user_id == user_id,
            NotificationPreference.user_role_context.in_((role, "both")),
            NotificationPreference.category == category,
        )
        .first()
    )
    if not row:
        return True, True
    return (True if mandatory else row.in_app_enabled), row.push_enabled


def enqueue_notification(
    db: Session,
    *,
    user_id: str,
    role: str,
    category: str,
    notification_type: str,
    title: str,
    body: str,
    title_zh: str,
    body_zh: str,
    business_type: str,
    business_id: str,
    deep_link: str,
    deduplication_key: str,
    mandatory: bool = False,
) -> bool:
    if (
        db.query(NotificationDispatch)
        .filter(NotificationDispatch.deduplication_key == deduplication_key)
        .first()
    ):
        return False
    in_app, push = _preference(
        db,
        user_id=user_id,
        role=role,
        category=category,
        mandatory=mandatory,
    )
    notification: SystemNotification | None = None
    if in_app:
        notification = SystemNotification(
            user_id=user_id,
            category="order" if business_type == "order" else "follow",
            title=title,
            title_zh=title_zh,
            body=body,
            body_zh=body_zh,
            action_type=business_type,
            action_ref=business_id,
            user_role_context=role,
            notification_type=notification_type,
            business_type=business_type,
            business_id=business_id,
            deep_link=deep_link,
            push_status="pending" if push else "disabled",
        )
        db.add(notification)
        db.flush()
    db.add(
        NotificationDispatch(
            notification_id=notification.id if notification else None,
            user_id=user_id,
            channel="push" if push else "in_app",
            deduplication_key=deduplication_key,
            status="pending" if push else "disabled",
        )
    )
    return True


def _process_order_reminders(db: Session) -> int:
    now = utcnow()
    day = now.strftime("%Y-%m-%d")
    count = 0
    candidates = (
        db.query(Order)
        .filter(
            or_(
                (Order.status == "pendingPay") & (Order.created_at <= now - timedelta(minutes=30)),
                (Order.status == "pendingShip") & (Order.updated_at <= now - timedelta(hours=12)),
                (Order.status == "pendingReceive") & (Order.updated_at <= now - timedelta(hours=24)),
            )
        )
        .limit(1000)
        .all()
    )
    for order in candidates:
        if order.status == "pendingPay":
            count += int(
                enqueue_notification(
                    db,
                    user_id=order.buyer_id,
                    role="buyer",
            category="payment_update",
                    notification_type="unpaid_order_reminder",
                    title="Complete your payment",
                    body=f"Order #{order.id} is waiting for payment.",
                    title_zh="请完成付款",
                    body_zh=f"订单 #{order.id} 正在等待付款。",
                    business_type="order",
                    business_id=str(order.id),
                    deep_link=f"heymarket://order/{order.id}",
                    deduplication_key=f"order:{order.id}:unpaid:{day}",
                    mandatory=False,
                )
            )
        elif order.status == "pendingShip":
            count += int(
                enqueue_notification(
                    db,
                    user_id=order.seller_id,
                    role="seller",
                    category="delivery_update",
                    notification_type="shipment_reminder",
                    title="Please arrange delivery",
                    body=f"Paid order #{order.id} is waiting for shipment or handover.",
                    title_zh="请安排交付",
                    body_zh=f"已付款订单 #{order.id} 正在等待发货或交接。",
                    business_type="order",
                    business_id=str(order.id),
                    deep_link=f"heymarket://order/{order.id}",
                    deduplication_key=f"order:{order.id}:ship:{day}",
                    mandatory=True,
                )
            )
        elif order.status == "pendingReceive":
            count += int(
                enqueue_notification(
                    db,
                    user_id=order.buyer_id,
                    role="buyer",
                    category="delivery_update",
                    notification_type="receipt_confirmation_reminder",
                    title="Confirm delivery when received",
                    body=f"Order #{order.id} is waiting for your delivery confirmation.",
                    title_zh="收到后请确认收货",
                    body_zh=f"订单 #{order.id} 正在等待您的收货确认。",
                    business_type="order",
                    business_id=str(order.id),
                    deep_link=f"heymarket://order/{order.id}",
                    deduplication_key=f"order:{order.id}:receive:{day}",
                    mandatory=True,
                )
            )
    return count


def _process_interest_notifications(db: Session) -> int:
    """Notify a bounded set of interested users once per newly approved listing."""
    now = utcnow()
    listings = (
        db.query(Listing)
        .filter(
            Listing.status == "active",
            Listing.review_status == "approved",
            Listing.created_at >= now - timedelta(hours=6),
        )
        .order_by(Listing.created_at.desc())
        .limit(100)
        .all()
    )
    count = 0
    for listing in listings:
        favorite_user_ids = (
            db.query(Favorite.user_id)
            .join(Listing, Favorite.listing_id == Listing.id)
            .filter(Listing.category_key == listing.category_key)
        )
        viewed_user_ids = (
            db.query(ViewHistory.user_id)
            .join(Listing, ViewHistory.listing_id == Listing.id)
            .filter(Listing.category_key == listing.category_key)
        )
        followed_seller_ids = db.query(Follow.follower_id).filter(
            Follow.followed_id == listing.seller_id
        )
        previous_conversation_ids = (
            db.query(Conversation.buyer_id)
            .join(Listing, Conversation.listing_id == Listing.id)
            .filter(Listing.category_key == listing.category_key)
        )
        previous_buyer_ids = (
            db.query(Order.buyer_id)
            .join(Listing, Order.listing_id == Listing.id)
            .filter(
                Listing.category_key == listing.category_key,
                Order.status.in_(("pendingShip", "pendingReceive", "pendingReview", "completed")),
            )
        )
        user_ids = {
            row[0]
            for row in favorite_user_ids
            .union(viewed_user_ids, followed_seller_ids, previous_conversation_ids, previous_buyer_ids)
            .limit(500)
            .all()
            if row[0] != listing.seller_id
        }
        searchable_text = f"{listing.title} {listing.title_zh or ''}".lower()
        search_rows = (
            db.query(SearchLog.user_id, SearchLog.term)
            .filter(
                SearchLog.user_id.is_not(None),
                SearchLog.created_at >= now - timedelta(days=30),
            )
            .order_by(SearchLog.created_at.desc())
            .limit(2000)
            .all()
        )
        user_ids.update(
            user_id
            for user_id, term in search_rows
            if user_id != listing.seller_id
            and term
            and term.lower() in searchable_text
        )
        users = (
            db.query(User)
            .filter(User.id.in_(user_ids), User.account_status == "normal")
            .all()
        )
        for interested_user in users:
            user_id = interested_user.id
            already_purchased = (
                db.query(Order.id)
                .filter(
                    Order.buyer_id == user_id,
                    Order.listing_id == listing.id,
                    Order.status.in_(("pendingShip", "pendingReceive", "pendingReview", "completed")),
                )
                .first()
            )
            seller_blocked = (
                db.query(BlocklistEntry.id)
                .filter(
                    BlocklistEntry.blocker_id == user_id,
                    BlocklistEntry.blocked_id == listing.seller_id,
                )
                .first()
            )
            if already_purchased or seller_blocked:
                continue
            if interested_user.city and listing.region_city and interested_user.city != listing.region_city:
                continue
            count += int(
                enqueue_notification(
                    db,
                    user_id=user_id,
                    role="buyer",
                    category="product_recommendation",
                    notification_type="interest_based_listing",
                    title="A new item may interest you",
                    body=listing.title[:180],
                    title_zh="您可能感兴趣的新商品",
                    body_zh=(listing.title_zh or listing.title)[:180],
                    business_type="listing",
                    business_id=str(listing.id),
                    deep_link=f"heymarket://listing/{listing.id}",
                    deduplication_key=f"listing:{listing.id}:interest:{user_id}",
                    mandatory=False,
                )
            )
    return count


def _deliver_pending_pushes(db: Session) -> tuple[int, int]:
    sent = 0
    failed = 0
    rows = (
        db.query(NotificationDispatch)
        .filter(
            NotificationDispatch.channel == "push",
            NotificationDispatch.status == "pending",
        )
        .order_by(NotificationDispatch.created_at.asc())
        .limit(200)
        .all()
    )
    for dispatch in rows:
        notification = (
            db.query(SystemNotification)
            .filter(SystemNotification.id == dispatch.notification_id)
            .first()
        )
        if not notification:
            dispatch.status = "failed"
            dispatch.failure_reason = "NOTIFICATION_NOT_FOUND"
            failed += 1
            continue
        user = db.query(User).filter(User.id == dispatch.user_id).first()
        use_chinese = bool(user and user.language.startswith("zh"))
        success, failure = send_generic_push(
            db,
            user_id=dispatch.user_id,
            title=(notification.title_zh if use_chinese else notification.title),
            body=(notification.body_zh if use_chinese else notification.body),
            data={
                "type": notification.business_type,
                "businessId": notification.business_id,
                "deepLink": notification.deep_link,
                "role": notification.user_role_context,
            },
        )
        dispatch.attempt_count += 1
        dispatch.last_attempt_at = utcnow()
        if success:
            dispatch.status = "sent"
            dispatch.sent_at = utcnow()
            notification.push_status = "sent"
            sent += 1
        else:
            dispatch.status = "failed" if dispatch.attempt_count >= 3 else "pending"
            dispatch.failure_reason = failure
            notification.push_status = dispatch.status
            failed += 1
    return sent, failed


def process_scheduled_notifications(db: Session) -> dict[str, int]:
    order_count = _process_order_reminders(db)
    interest_count = _process_interest_notifications(db)
    db.flush()
    push_sent, push_failed = _deliver_pending_pushes(db)
    db.commit()
    return {
        "orderReminders": order_count,
        "interestNotifications": interest_count,
        "pushSent": push_sent,
        "pushFailed": push_failed,
    }
