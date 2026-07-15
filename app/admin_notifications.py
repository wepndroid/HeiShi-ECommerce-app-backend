"""Durable real-time inbox events for the admin dashboard."""

from queue import Queue
from threading import Lock

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models import AdminNotification

_subscribers: set[Queue] = set()
_subscribers_lock = Lock()


def notification_payload(row: AdminNotification) -> dict:
    return {
        "id": row.id,
        "eventType": row.event_type,
        "title": row.title,
        "body": row.body,
        "targetType": row.target_type,
        "targetId": row.target_id,
        "actionPath": row.action_path,
        "isRead": bool(row.is_read),
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


def subscribe_admin_notifications() -> Queue:
    subscriber: Queue = Queue()
    with _subscribers_lock:
        _subscribers.add(subscriber)
    return subscriber


def unsubscribe_admin_notifications(subscriber: Queue) -> None:
    with _subscribers_lock:
        _subscribers.discard(subscriber)


@event.listens_for(Session, "after_commit")
def _publish_committed_admin_notifications(session: Session) -> None:
    payloads = session.info.pop("admin_notification_payloads", [])
    if not payloads:
        return
    with _subscribers_lock:
        subscribers = tuple(_subscribers)
    for payload in payloads:
        for subscriber in subscribers:
            subscriber.put(payload)


@event.listens_for(Session, "after_rollback")
def _discard_rolled_back_admin_notifications(session: Session) -> None:
    session.info.pop("admin_notification_payloads", None)


def notify_admin(
    db: Session,
    *,
    event_type: str,
    title: str,
    body: str,
    target_type: str,
    target_id: str | int,
    action_path: str,
) -> AdminNotification:
    notification = AdminNotification(
        event_type=event_type,
        title=title,
        body=body,
        target_type=target_type,
        target_id=str(target_id),
        action_path=action_path,
    )
    db.add(notification)
    db.flush()
    db.info.setdefault("admin_notification_payloads", []).append(notification_payload(notification))
    return notification
