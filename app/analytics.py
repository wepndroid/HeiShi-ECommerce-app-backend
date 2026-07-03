"""Platform analytics: DAU and promotion clicks."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import DailyActiveUser, DailyActiveUserHit, Listing, PromotionClickEvent


def record_daily_active_user(db: Session, user_id: str) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    existing = (
        db.query(DailyActiveUserHit)
        .filter(DailyActiveUserHit.user_id == user_id, DailyActiveUserHit.day == today)
        .first()
    )
    if existing:
        return
    db.add(DailyActiveUserHit(user_id=user_id, day=today))
    row = db.query(DailyActiveUser).filter(DailyActiveUser.day == today).first()
    if row is None:
        db.add(DailyActiveUser(day=today, user_count=1))
    else:
        row.user_count = (row.user_count or 0) + 1
    db.commit()


def record_promotion_click(db: Session, listing_id: int, user_id: str | None = None) -> None:
    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if not listing or not (listing.is_pinned or listing.is_recommended):
        return
    db.add(PromotionClickEvent(listing_id=listing_id, user_id=user_id))
    listing.promotion_click_count = (listing.promotion_click_count or 0) + 1
    db.commit()
