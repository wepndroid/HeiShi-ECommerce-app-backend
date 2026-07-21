"""Idempotent scheduled notifications for transaction and interest events."""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    BlocklistEntry,
    Conversation,
    Favorite,
    Follow,
    FollowedCategory,
    Listing,
    Message,
    NotificationDispatch,
    NotificationPreference,
    Order,
    PrivateOffer,
    SearchLog,
    SystemNotification,
    User,
    UserSettings,
    ViewHistory,
    ensure_utc,
    utcnow,
)
from app.push_notifications import send_generic_push
from app.sms_notifications import send_transaction_sms


def _preference(
    db: Session,
    *,
    user_id: str,
    role: str,
    category: str,
    mandatory: bool,
) -> tuple[bool, bool, bool]:
    # A role-specific choice must override the user's broader "both" choice.
    # Do not rely on database row order when both records exist.
    row = (
        db.query(NotificationPreference)
        .filter(
            NotificationPreference.user_id == user_id,
            NotificationPreference.user_role_context == role,
            NotificationPreference.category == category,
        )
        .first()
    )
    if not row:
        row = (
            db.query(NotificationPreference)
            .filter(
                NotificationPreference.user_id == user_id,
                NotificationPreference.user_role_context == "both",
                NotificationPreference.category == category,
            )
            .first()
        )
    if not row:
        return True, True, False
    return (
        True if mandatory else row.in_app_enabled,
        row.push_enabled,
        row.sms_enabled,
    )


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
    existing = (
        db.query(NotificationDispatch)
        .filter(
            or_(
                NotificationDispatch.deduplication_key == deduplication_key,
                NotificationDispatch.deduplication_key.startswith(
                    f"{deduplication_key}:",
                    autoescape=True,
                ),
            )
        )
        .first()
    )
    if existing:
        return False
    in_app, push, sms = _preference(
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
            notification_category=category,
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
    payload_json = json.dumps(
        {
            "title": title,
            "titleZh": title_zh,
            "body": body,
            "bodyZh": body_zh,
            "businessType": business_type,
            "businessId": business_id,
            "deepLink": deep_link,
            "role": role,
        },
        ensure_ascii=False,
    )
    channels = [channel for channel, enabled in (("push", push), ("sms", sms)) if enabled]
    if not channels:
        channels = ["in_app"]
    for channel in channels:
        db.add(
            NotificationDispatch(
                notification_id=notification.id if notification else None,
                user_id=user_id,
                channel=channel,
                deduplication_key=f"{deduplication_key}:{channel}",
                status="pending" if channel in {"push", "sms"} else "disabled",
                payload_json=payload_json,
            )
        )
    return True


def notify_payment_failed(
    db: Session,
    order: Order,
    *,
    reason: str | None = None,
    event_key: str = "provider",
) -> bool:
    safe_reason = (reason or "The payment provider declined or could not complete the payment.")[:240]
    return enqueue_notification(
        db,
        user_id=order.buyer_id,
        role="buyer",
        category="payment_update",
        notification_type="order_payment_failed",
        title="Payment did not complete",
        body=f"Payment for order #{order.id} did not complete. {safe_reason}",
        title_zh="付款未完成",
        body_zh=f"订单 #{order.id} 的付款未完成。请检查付款方式后重试。",
        business_type="order",
        business_id=str(order.id),
        deep_link=f"heymarket://order/{order.id}",
        deduplication_key=f"order:{order.id}:payment:failed:{event_key}",
        mandatory=True,
    )


def notify_seller_new_order(db: Session, order: Order, listing_title: str) -> bool:
    return enqueue_notification(
        db,
        user_id=order.seller_id,
        role="seller",
        category="order_update",
        notification_type="buyer_submitted_order",
        title="A buyer submitted an order",
        body=f"Order #{order.id} for {listing_title[:120]} is awaiting buyer payment.",
        title_zh="买家已提交订单",
        body_zh=f"订单 #{order.id} 已提交，正在等待买家付款。",
        business_type="order",
        business_id=str(order.id),
        deep_link=f"heymarket://order/{order.id}",
        deduplication_key=f"order:{order.id}:submitted:seller",
        mandatory=False,
    )


def notify_seller_interest_milestone(
    db: Session,
    listing: Listing,
    *,
    signal: str,
    count: int,
) -> bool:
    return enqueue_notification(
        db,
        user_id=listing.seller_id,
        role="seller",
        category="product_recommendation",
        notification_type="listing_significant_interest",
        title="Your listing is attracting interest",
        body=f"{listing.title[:120]} has reached {count} {signal}.",
        title_zh="您的商品受到关注",
        body_zh=f"“{(listing.title_zh or listing.title)[:120]}”已获得 {count} 次{signal}。",
        business_type="listing",
        business_id=str(listing.id),
        deep_link=f"heymarket://listing/{listing.id}",
        deduplication_key=f"listing:{listing.id}:interest:{signal}:{count}",
        mandatory=False,
    )


def notify_listing_available_again(
    db: Session,
    listing: Listing,
    *,
    transition_key: str,
) -> int:
    user_ids = {
        user_id
        for (user_id,) in db.query(Favorite.user_id)
        .filter(
            Favorite.listing_id == listing.id,
            Favorite.user_id != listing.seller_id,
        )
        .all()
    }
    count = 0
    for user_id in user_ids:
        count += int(
            enqueue_notification(
                db,
                user_id=user_id,
                role="buyer",
                category="product_recommendation",
                notification_type="favorite_available_again",
                title="A saved item is available again",
                body=f"{listing.title[:120]} is available again.",
                title_zh="收藏商品重新上架",
                body_zh=f"“{(listing.title_zh or listing.title)[:120]}”现在可以再次购买。",
                business_type="listing",
                business_id=str(listing.id),
                deep_link=f"heymarket://listing/{listing.id}",
                deduplication_key=(
                    f"listing:{listing.id}:available-again:{transition_key}:{user_id}"
                ),
                mandatory=False,
            )
        )
    return count


def notify_payout_transition(
    db: Session,
    order: Order,
    *,
    status: str,
    reference: str | None,
    reason: str | None,
) -> bool:
    labels = {
        "processing": (
            "Seller payout is processing",
            "卖家结算处理中",
            f"Settlement for order #{order.id} is being processed.",
            f"订单 #{order.id} 的卖家结算正在处理中。",
        ),
        "released": (
            "Seller payout completed",
            "卖家结算已完成",
            f"Settlement for order #{order.id} has been released.",
            f"订单 #{order.id} 的卖家结算已完成。",
        ),
        "failed": (
            "Seller payout failed",
            "卖家结算失败",
            f"Settlement for order #{order.id} failed and requires attention.",
            f"订单 #{order.id} 的卖家结算失败，需要处理。",
        ),
        "blocked": (
            "Seller payout is blocked",
            "卖家结算已暂停",
            f"Settlement for order #{order.id} is blocked. {(reason or '')[:160]}",
            f"订单 #{order.id} 的卖家结算已暂停，请检查收款账户或订单状态。",
        ),
        "reversed": (
            "Seller payout was reversed",
            "卖家结算已撤回",
            f"Settlement for order #{order.id} was reversed.",
            f"订单 #{order.id} 的卖家结算已撤回。",
        ),
    }
    if status not in labels:
        return False
    title, title_zh, body, body_zh = labels[status]
    key_reference = reference or (reason or "")[:40] or "none"
    return enqueue_notification(
        db,
        user_id=order.seller_id,
        role="seller",
        category="payout",
        notification_type=f"seller_payout_{status}",
        title=title,
        body=body,
        title_zh=title_zh,
        body_zh=body_zh,
        business_type="order",
        business_id=str(order.id),
        deep_link=f"heymarket://order/{order.id}",
        deduplication_key=f"order:{order.id}:payout:{status}:{key_reference}",
        mandatory=True,
    )


def _process_order_reminders(db: Session) -> int:
    # Expiry is a server state transition, not a side effect of visiting a feed.
    # Run it before selecting reminder candidates so an expired unpaid order can
    # never receive a misleading payment reminder.
    from app.catalog_helpers import expire_stale_pending_pay_orders

    expire_stale_pending_pay_orders(db, settings.pending_pay_expire_minutes)
    now = utcnow()
    day = now.strftime("%Y-%m-%d")
    count = 0
    candidates = (
        db.query(Order)
        .filter(
            or_(
                (Order.status == "pendingPay")
                & (
                    Order.created_at
                    <= now - timedelta(minutes=settings.pending_pay_reminder_minutes)
                ),
                (Order.status == "pendingShip") & (Order.updated_at <= now - timedelta(hours=12)),
                (Order.status == "pendingReceive") & (Order.updated_at <= now - timedelta(hours=24)),
            )
        )
        .yield_per(500)
    )
    for order in candidates:
        reminder_settings = (
            db.query(UserSettings)
            .filter(
                UserSettings.user_id
                == (order.seller_id if order.status == "pendingShip" else order.buyer_id)
            )
            .first()
        )
        if order.status == "pendingPay":
            if reminder_settings and not reminder_settings.remind_pay:
                continue
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
            deadline_threshold_minutes = max(
                settings.pending_pay_expire_minutes
                - settings.pending_pay_deadline_reminder_minutes_before,
                settings.pending_pay_reminder_minutes,
            )
            if ensure_utc(order.created_at) <= now - timedelta(
                minutes=deadline_threshold_minutes
            ):
                minutes_left = max(
                    settings.pending_pay_expire_minutes - deadline_threshold_minutes,
                    1,
                )
                count += int(
                    enqueue_notification(
                        db,
                        user_id=order.buyer_id,
                        role="buyer",
                        category="payment_update",
                        notification_type="payment_deadline_reminder",
                        title="Payment deadline approaching",
                        body=(
                            f"Complete payment for order #{order.id} within about "
                            f"{minutes_left} minutes to keep the order."
                        ),
                        title_zh="付款截止时间临近",
                        body_zh=(
                            f"请在约 {minutes_left} 分钟内完成订单 #{order.id} 的付款，"
                            "否则订单将自动取消。"
                        ),
                        business_type="order",
                        business_id=str(order.id),
                        deep_link=f"heymarket://order/{order.id}",
                        deduplication_key=f"order:{order.id}:payment-deadline",
                        mandatory=False,
                    )
                )
        elif order.status == "pendingShip":
            if reminder_settings and not reminder_settings.remind_ship:
                continue
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
            if reminder_settings and not reminder_settings.remind_receive:
                continue
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
    from app.catalog_helpers import listing_excluded_from_recommendations

    now = utcnow()
    listings = (
        db.query(Listing)
        .join(User, User.id == Listing.seller_id)
        .filter(
            Listing.status == "active",
            Listing.review_status == "approved",
            User.account_status == "normal",
            Listing.created_at >= now - timedelta(hours=6),
        )
        .order_by(Listing.created_at.desc())
        .yield_per(100)
    )
    count = 0
    for listing in listings:
        if listing_excluded_from_recommendations(db, listing):
            continue
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
        followed_category_ids = db.query(FollowedCategory.user_id).filter(
            FollowedCategory.category_key == listing.category_key
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
                Order.status.in_(
                    ("pendingShip", "pendingService", "pendingReceive", "pendingReview", "completed")
                ),
            )
        )
        user_ids = {
            row[0]
            for row in favorite_user_ids
            .union(
                viewed_user_ids,
                followed_seller_ids,
                followed_category_ids,
                previous_conversation_ids,
                previous_buyer_ids,
            )
            .yield_per(1000)
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
            .yield_per(1000)
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
            .outerjoin(UserSettings, UserSettings.user_id == User.id)
            .filter(User.id.in_(user_ids), User.account_status == "normal")
            .filter(
                or_(
                    UserSettings.user_id.is_(None),
                    UserSettings.personalization.is_(True),
                )
            )
            .all()
        )
        for interested_user in users:
            user_id = interested_user.id
            if listing_excluded_from_recommendations(
                db,
                listing,
                interested_user.city,
            ):
                continue
            already_purchased = (
                db.query(Order.id)
                .filter(
                    Order.buyer_id == user_id,
                    Order.listing_id == listing.id,
                    Order.status.in_(
                        ("pendingShip", "pendingService", "pendingReceive", "pendingReview", "completed")
                    ),
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


def _process_expired_private_offers(db: Session) -> int:
    """Expire untouched offers and synchronize their structured chat cards."""
    now = utcnow()
    offers = (
        db.query(PrivateOffer)
        .filter(
            PrivateOffer.status.in_(("PENDING", "VIEWED")),
            PrivateOffer.expiration_time <= now,
        )
        .order_by(PrivateOffer.expiration_time.asc())
        .limit(500)
        .all()
    )
    expired = 0
    for offer in offers:
        offer.status = "EXPIRED"
        messages = (
            db.query(Message)
            .filter(
                Message.conversation_id == offer.conversation_id,
                Message.message_type == "private_offer",
            )
            .all()
        )
        for message in messages:
            try:
                payload = json.loads(message.structured_payload_json or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if payload.get("id") != offer.id:
                continue
            payload["status"] = "EXPIRED"
            message.structured_payload_json = json.dumps(payload)
        enqueue_notification(
            db,
            user_id=offer.seller_id,
            role="seller",
            category="order_update",
            notification_type="private_offer_expired",
            title="Private offer expired",
            body="A buyer-specific offer expired without acceptance.",
            title_zh="专属报价已过期",
            body_zh="一个买家专属报价已过期且未被接受。",
            business_type="offer",
            business_id=offer.id,
            deep_link=f"heymarket://chat/{offer.conversation_id}",
            deduplication_key=f"offer:{offer.id}:expired",
        )
        expired += 1
    return expired


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
        try:
            stored_payload = json.loads(dispatch.payload_json or "{}")
        except (TypeError, json.JSONDecodeError):
            stored_payload = {}
        if not notification and not stored_payload:
            dispatch.status = "failed"
            dispatch.failure_reason = "NOTIFICATION_PAYLOAD_NOT_FOUND"
            failed += 1
            continue
        user = db.query(User).filter(User.id == dispatch.user_id).first()
        use_chinese = bool(user and user.language.startswith("zh"))
        title = (
            notification.title_zh if use_chinese else notification.title
        ) if notification else (
            stored_payload.get("titleZh") if use_chinese else stored_payload.get("title")
        )
        body = (
            notification.body_zh if use_chinese else notification.body
        ) if notification else (
            stored_payload.get("bodyZh") if use_chinese else stored_payload.get("body")
        )
        data = {
            "type": (
                notification.business_type
                if notification
                else stored_payload.get("businessType")
            ),
            "businessId": (
                notification.business_id
                if notification
                else stored_payload.get("businessId")
            ),
            "deepLink": (
                notification.deep_link
                if notification
                else stored_payload.get("deepLink")
            ),
            "role": (
                notification.user_role_context
                if notification
                else stored_payload.get("role")
            ),
        }
        success, failure = send_generic_push(
            db,
            user_id=dispatch.user_id,
            title=str(title or ""),
            body=str(body or ""),
            data=data,
        )
        dispatch.attempt_count += 1
        dispatch.last_attempt_at = utcnow()
        if success:
            dispatch.status = "sent"
            dispatch.sent_at = utcnow()
            if notification:
                notification.push_status = "sent"
            sent += 1
        else:
            dispatch.status = "failed" if dispatch.attempt_count >= 3 else "pending"
            dispatch.failure_reason = failure
            if notification:
                notification.push_status = dispatch.status
            failed += 1
    return sent, failed


def _deliver_pending_sms(db: Session) -> tuple[int, int]:
    sent = 0
    failed = 0
    rows = (
        db.query(NotificationDispatch)
        .filter(
            NotificationDispatch.channel == "sms",
            NotificationDispatch.status == "pending",
        )
        .order_by(NotificationDispatch.created_at.asc())
        .limit(200)
        .all()
    )
    for dispatch in rows:
        user = db.query(User).filter(User.id == dispatch.user_id).first()
        try:
            payload = json.loads(dispatch.payload_json or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        use_chinese = bool(user and user.language.startswith("zh"))
        title = payload.get("titleZh") if use_chinese else payload.get("title")
        body = payload.get("bodyZh") if use_chinese else payload.get("body")
        message = " — ".join(value for value in (str(title or ""), str(body or "")) if value)
        success, failure = send_transaction_sms(
            phone=user.phone if user else None,
            body=message,
        )
        dispatch.attempt_count += 1
        dispatch.last_attempt_at = utcnow()
        if success:
            dispatch.status = "sent"
            dispatch.sent_at = utcnow()
            sent += 1
        else:
            dispatch.status = "failed" if dispatch.attempt_count >= 3 else "pending"
            dispatch.failure_reason = failure
            failed += 1
    return sent, failed


def process_scheduled_notifications(db: Session) -> dict[str, int]:
    order_count = _process_order_reminders(db)
    expired_offer_count = _process_expired_private_offers(db)
    interest_count = _process_interest_notifications(db)
    db.flush()
    push_sent, push_failed = _deliver_pending_pushes(db)
    sms_sent, sms_failed = _deliver_pending_sms(db)
    db.commit()
    return {
        "orderReminders": order_count,
        "expiredPrivateOffers": expired_offer_count,
        "interestNotifications": interest_count,
        "pushSent": push_sent,
        "pushFailed": push_failed,
        "smsSent": sms_sent,
        "smsFailed": sms_failed,
    }
