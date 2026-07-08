"""Background order maintenance: auto-confirm after buyer inactivity."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import Order
from app.payout_release import release_payout_for_order

AUTO_CONFIRM_DAYS = 5


def schedule_auto_confirm(order: Order) -> None:
    """Set auto-confirm deadline when order enters pendingReceive."""
    order.auto_confirm_at = datetime.now(timezone.utc) + timedelta(days=AUTO_CONFIRM_DAYS)


def process_auto_confirm_orders(db: Session) -> int:
    """Confirm receipt for orders past auto_confirm_at with no open dispute."""
    now = datetime.now(timezone.utc)
    rows = (
        db.query(Order)
        .filter(
            Order.status.in_(("pendingReceive", "pendingService")),
            Order.auto_confirm_at.isnot(None),
            Order.auto_confirm_at <= now,
            Order.payout_paused.is_(False),
        )
        .all()
    )
    count = 0
    for order in rows:
        if order.dispute_status == "open":
            continue
        order.status = "pendingReview"
        release_payout_for_order(db, order)
        order.confirmed_at = now
        order.updated_at = now
        count += 1
    if count:
        db.commit()
    return count
