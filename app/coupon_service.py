"""Issue and maintain user coupons."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import Coupon

COUPON_KIND_WELCOME = "welcome"
COUPON_KIND_REFERRAL = "referral"

WELCOME_AMOUNT = 5.0
WELCOME_VALID_DAYS = 90
REFERRAL_AMOUNT = 10.0
REFERRAL_VALID_DAYS = 60


def _welcome_description(language: str) -> str:
    if language == "zh":
        return "新手礼券 立减 A$5"
    return "Welcome coupon A$5 off"


def _referral_description(language: str) -> str:
    if language == "zh":
        return "邀请好友奖励 A$10"
    return "Referral bonus A$10"


def _has_coupon_kind(db: Session, user_id: str, kind: str) -> bool:
    return (
        db.query(Coupon.id)
        .filter(Coupon.user_id == user_id, Coupon.kind == kind)
        .first()
        is not None
    )


def refresh_expired_coupons(db: Session, user_id: str) -> None:
    now = datetime.now(timezone.utc)
    (
        db.query(Coupon)
        .filter(
            Coupon.user_id == user_id,
            Coupon.status == "available",
            Coupon.expires_at.isnot(None),
            Coupon.expires_at < now,
        )
        .update({Coupon.status: "expired"}, synchronize_session=False)
    )


def issue_coupon(
    db: Session,
    user_id: str,
    *,
    amount: float,
    description: str,
    kind: str,
    valid_days: int,
) -> Coupon | None:
    if _has_coupon_kind(db, user_id, kind):
        return None
    now = datetime.now(timezone.utc)
    coupon = Coupon(
        user_id=user_id,
        amount=amount,
        description=description,
        kind=kind,
        expires_at=now + timedelta(days=valid_days),
        status="available",
    )
    db.add(coupon)
    return coupon


def issue_welcome_coupon(db: Session, user_id: str, language: str = "en") -> Coupon | None:
    lang = "zh" if language == "zh" else "en"
    return issue_coupon(
        db,
        user_id,
        amount=WELCOME_AMOUNT,
        description=_welcome_description(lang),
        kind=COUPON_KIND_WELCOME,
        valid_days=WELCOME_VALID_DAYS,
    )


def issue_referral_coupon(db: Session, user_id: str, language: str = "en") -> Coupon | None:
    lang = "zh" if language == "zh" else "en"
    return issue_coupon(
        db,
        user_id,
        amount=REFERRAL_AMOUNT,
        description=_referral_description(lang),
        kind=COUPON_KIND_REFERRAL,
        valid_days=REFERRAL_VALID_DAYS,
    )
