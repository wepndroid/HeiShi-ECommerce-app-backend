"""Deliver chat alerts via the Expo Push API (works when the app is closed)."""

from __future__ import annotations

import logging

import httpx
from sqlalchemy.orm import Session

from app.catalog_helpers import get_or_create_settings
from app.models import DevicePushToken

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
CHAT_CHANNEL_ID = "chat-messages"
ORDER_CHANNEL_ID = "order-updates"


def _chat_push_title(sender_name: str, lang: str) -> str:
    if lang.startswith("zh"):
        return f"{sender_name} 发来新消息"
    return f"New message from {sender_name}"


def _chat_push_body(message_preview: str, lang: str) -> str:
    trimmed = message_preview.strip()
    if trimmed:
        return trimmed[:160]
    return "新消息" if lang.startswith("zh") else "New message"


def send_order_remind_push(
    db: Session,
    *,
    seller_id: str,
    buyer_name: str,
    order_id: int,
    listing_title: str,
    lang: str = "en",
) -> None:
    settings = get_or_create_settings(db, seller_id)
    if not settings.remind_ship:
        return

    rows = db.query(DevicePushToken).filter(DevicePushToken.user_id == seller_id).all()
    if not rows:
        return

    title = "发货提醒" if lang.startswith("zh") else "Ship reminder"
    body = (
        f"买家 {buyer_name} 提醒你发货：{listing_title[:80]}"
        if lang.startswith("zh")
        else f"{buyer_name} reminded you to ship: {listing_title[:80]}"
    )
    messages: list[dict] = []
    for row in rows:
        payload: dict = {
            "to": row.token,
            "title": title,
            "body": body,
            "data": {"orderId": order_id, "type": "order"},
            "sound": "default",
            "priority": "high",
        }
        if row.platform == "android":
            payload["channelId"] = ORDER_CHANNEL_ID
        messages.append(payload)

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                EXPO_PUSH_URL,
                json=messages,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            if response.status_code != 200:
                logger.warning("Expo remind-ship HTTP %s: %s", response.status_code, response.text[:200])
    except Exception as exc:
        logger.warning("Expo remind-ship request failed: %s", exc)


def send_order_paid_push(
    db: Session,
    *,
    seller_id: str,
    buyer_name: str,
    order_id: int,
    listing_title: str,
    lang: str = "en",
) -> None:
    settings = get_or_create_settings(db, seller_id)
    if not settings.remind_pay:
        return

    rows = db.query(DevicePushToken).filter(DevicePushToken.user_id == seller_id).all()
    if not rows:
        return

    title = "新订单" if lang.startswith("zh") else "New order"
    body = (
        f"买家 {buyer_name} 已付款：{listing_title[:80]}，请尽快发货"
        if lang.startswith("zh")
        else f"{buyer_name} paid for {listing_title[:80]} — please ship"
    )
    messages: list[dict] = []
    for row in rows:
        payload: dict = {
            "to": row.token,
            "title": title,
            "body": body,
            "data": {"orderId": order_id, "type": "order", "filter": "pendingShip"},
            "sound": "default",
            "priority": "high",
        }
        if row.platform == "android":
            payload["channelId"] = ORDER_CHANNEL_ID
        messages.append(payload)

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                EXPO_PUSH_URL,
                json=messages,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            if response.status_code != 200:
                logger.warning("Expo order-paid HTTP %s: %s", response.status_code, response.text[:200])
    except Exception as exc:
        logger.warning("Expo order-paid request failed: %s", exc)


def send_chat_message_push(
    db: Session,
    *,
    recipient_id: str,
    sender_name: str,
    message_preview: str,
    conversation_id: str,
    lang: str = "en",
) -> None:
    settings = get_or_create_settings(db, recipient_id)
    if not settings.chat_messages:
        return

    rows = db.query(DevicePushToken).filter(DevicePushToken.user_id == recipient_id).all()
    if not rows:
        return

    title = _chat_push_title(sender_name, lang)
    body = _chat_push_body(message_preview, lang)
    messages: list[dict] = []
    for row in rows:
        payload: dict = {
            "to": row.token,
            "title": title,
            "body": body,
            "data": {"conversationId": conversation_id, "type": "chat"},
            "sound": "default",
            "priority": "high",
        }
        if row.platform == "android":
            payload["channelId"] = CHAT_CHANNEL_ID
        messages.append(payload)

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                EXPO_PUSH_URL,
                json=messages,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            if response.status_code != 200:
                logger.warning("Expo push HTTP %s: %s", response.status_code, response.text[:200])
                return
            payload = response.json()
    except Exception as exc:
        logger.warning("Expo push request failed: %s", exc)
        return

    tickets = payload.get("data")
    if not isinstance(tickets, list):
        return

    stale_tokens: list[str] = []
    for row, ticket in zip(rows, tickets):
        if not isinstance(ticket, dict) or ticket.get("status") != "error":
            continue
        details = ticket.get("details") or {}
        if details.get("error") == "DeviceNotRegistered":
            stale_tokens.append(row.token)

    if stale_tokens:
        db.query(DevicePushToken).filter(DevicePushToken.token.in_(stale_tokens)).delete(
            synchronize_session=False
        )
        db.commit()
