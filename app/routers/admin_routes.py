"""Admin API routes (/v1/admin/*) — PROG-402–407."""

from __future__ import annotations

import asyncio
import json
from queue import Empty
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.admin_audit import log_admin_action
from app.admin_notifications import (
    notification_payload,
    subscribe_admin_notifications,
    unsubscribe_admin_notifications,
)
from app.admin_auth import require_admin
from app.auth import create_access_token, create_refresh_token, hash_password, normalize_phone, store_refresh_token, verify_password
from app.config import settings
from app.database import SessionLocal, get_db
from app.models import (
    AdminNotification,
    BlockedKeyword,
    Conversation,
    DailyActiveUser,
    Favorite,
    Listing,
    Message,
    Order,
    PlatformBanner,
    PlatformCategory,
    PlatformRegion,
    PlatformSetting,
    PlatformTopic,
    ProductTag,
    PromotionClickEvent,
    ReportReason,
    Review,
    SafetyReport,
    SearchLog,
    User,
    VerificationSubmission,
    ViewHistory,
)
from app.media_urls import normalize_media_urls
from app.payments.refunds import refund_order_payment
from app.payout_release import release_payout_for_order, reverse_released_payout_for_order
from app.serializers import user_to_dto, _user_avatar_url
from app.schemas import AuthTokensDto, AuthUserDto

router = APIRouter(prefix="/admin", tags=["admin"])


def _admin_notification_dto(row: AdminNotification) -> dict:
    return notification_payload(row)


@router.get("/notifications")
def list_admin_notifications(
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    rows = db.query(AdminNotification).order_by(AdminNotification.created_at.desc()).limit(limit).all()
    unread_count = db.query(func.count(AdminNotification.id)).filter(AdminNotification.is_read.is_(False)).scalar() or 0
    return {"items": [_admin_notification_dto(row) for row in rows], "unreadCount": unread_count}


@router.post("/notifications/{notification_id}/read", status_code=204)
def mark_admin_notification_read(
    notification_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    row = db.query(AdminNotification).filter(AdminNotification.id == notification_id).first()
    if not row:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Notification not found", "details": {}})
    row.is_read = True
    db.commit()


@router.post("/notifications/read-all", status_code=204)
def mark_all_admin_notifications_read(
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    db.query(AdminNotification).filter(AdminNotification.is_read.is_(False)).update({"is_read": True})
    db.commit()


@router.get("/notifications/stream")
async def stream_admin_notifications(request: Request, admin: User = Depends(require_admin)):
    async def events():
        subscriber = subscribe_admin_notifications()
        seen_ids: set[str] = set()
        db = SessionLocal()
        try:
            rows = (
                db.query(AdminNotification)
                .filter(AdminNotification.is_read.is_(False))
                .order_by(AdminNotification.created_at.asc())
                .all()
            )
            for row in rows:
                seen_ids.add(row.id)
                yield f"data: {json.dumps(_admin_notification_dto(row))}\n\n"
        finally:
            db.close()

        idle_ticks = 0
        try:
            while not await request.is_disconnected():
                try:
                    payload = await asyncio.to_thread(subscriber.get, True, 1.0)
                except Empty:
                    payload = None
                if payload and payload["id"] not in seen_ids:
                    seen_ids.add(payload["id"])
                    yield f"data: {json.dumps(payload)}\n\n"
                idle_ticks += 1
                if idle_ticks >= 15:
                    idle_ticks = 0
                    yield ": keep-alive\n\n"
        finally:
            unsubscribe_admin_notifications(subscriber)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

def _visible_admin_nickname(nickname: str | None) -> str | None:
    if not nickname:
        return None
    return nickname.strip().lower()


def _is_visible_admin_user(user: User | None) -> bool:
    return user is not None


def _visible_admin_users(rows: list[User]) -> list[User]:
    return [user for user in rows if _is_visible_admin_user(user)]


class AdminLoginRequest(BaseModel):
    phone: str
    password: str


class AdminNoteRequest(BaseModel):
    note: str


class RejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class ContentEditRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    categoryKey: str | None = None


class ContentNoteRequest(BaseModel):
    note: str = Field(min_length=1, max_length=500)


class ContentFlagsRequest(BaseModel):
    recommended: bool | None = None
    pinned: bool | None = None


class ReportActionRequest(BaseModel):
    action: str = Field(pattern="^(ignore|warn|remove_content|ban_user|restore_content)$")
    note: str = Field(default="", max_length=500)


class DisputeResolveRequest(BaseModel):
    resolution: str = Field(pattern="^(refund|complete)$")
    note: str = Field(default="", max_length=500)


class UserModerateRequest(BaseModel):
    """Shared body for mute / restrict-publish / flag actions (reason optional)."""

    reason: str = Field(default="", max_length=500)


class CategoryUpsertRequest(BaseModel):
    type: str = Field(pattern="^(product|service|job|rental)$")
    key: str = Field(min_length=1, max_length=50)
    labelEn: str
    labelZh: str
    sortOrder: int = 0
    enabled: bool = True
    icon: str | None = None
    showOnHome: bool = True


class CategoryPatchRequest(BaseModel):
    labelEn: str | None = None
    labelZh: str | None = None
    sortOrder: int | None = None
    enabled: bool | None = None
    icon: str | None = None
    showOnHome: bool | None = None


class ContentTagsRequest(BaseModel):
    tagKey: str = Field(default="", max_length=50)


class ReviewModerateRequest(BaseModel):
    note: str = Field(default="", max_length=500)


class KeywordUpsertRequest(BaseModel):
    pattern: str = Field(min_length=1, max_length=200)
    locale: str = Field(default="all", max_length=5)
    active: bool = True


class KeywordPatchRequest(BaseModel):
    pattern: str | None = None
    locale: str | None = None
    active: bool | None = None


class ReportReasonUpsertRequest(BaseModel):
    key: str = Field(min_length=1, max_length=50)
    labelEn: str
    labelZh: str
    sortOrder: int = 0
    active: bool = True


class ReportReasonPatchRequest(BaseModel):
    labelEn: str | None = None
    labelZh: str | None = None
    sortOrder: int | None = None
    active: bool | None = None


class ProductTagUpsertRequest(BaseModel):
    key: str = Field(min_length=1, max_length=50)
    labelEn: str
    labelZh: str
    sortOrder: int = 0
    active: bool = True


class ProductTagPatchRequest(BaseModel):
    labelEn: str | None = None
    labelZh: str | None = None
    sortOrder: int | None = None
    active: bool | None = None


class SettingsPatchRequest(BaseModel):
    """Upsert a batch of key/value platform settings (home switches, ToS, privacy)."""

    values: dict[str, str]


class TopicUpsertRequest(BaseModel):
    title: str
    titleZh: str | None = None
    subtitle: str | None = None
    coverImageUrl: str = ""
    tagKey: str | None = None
    linkUrl: str | None = None
    onlineAt: str | None = None
    offlineAt: str | None = None
    sortOrder: int = 0
    enabled: bool = True


class TopicPatchRequest(BaseModel):
    title: str | None = None
    titleZh: str | None = None
    subtitle: str | None = None
    coverImageUrl: str | None = None
    tagKey: str | None = None
    linkUrl: str | None = None
    onlineAt: str | None = None
    offlineAt: str | None = None
    sortOrder: int | None = None
    enabled: bool | None = None


class RegionUpsertRequest(BaseModel):
    country: str = "AU"
    state: str
    city: str
    area: str | None = None
    labelEn: str
    labelZh: str
    isDefaultCity: bool = False
    sortOrder: int = 0
    enabled: bool = True


class RegionPatchRequest(BaseModel):
    labelEn: str | None = None
    labelZh: str | None = None
    isDefaultCity: bool | None = None
    sortOrder: int | None = None
    enabled: bool | None = None


class BannerUpsertRequest(BaseModel):
    title: str
    imageUrl: str
    linkUrl: str | None = None
    position: str = Field(default="home", pattern="^(home|category)$")
    onlineAt: str | None = None
    offlineAt: str | None = None
    enabled: bool = True


class BannerPatchRequest(BaseModel):
    title: str | None = None
    imageUrl: str | None = None
    linkUrl: str | None = None
    position: str | None = None
    onlineAt: str | None = None
    offlineAt: str | None = None
    enabled: bool | None = None


def _party(user: User | None) -> dict | None:
    """Canonical embedded-user shape — mirrors the mobile app's nested seller/buyer Party."""
    if user is None:
        return None
    return {
        "id": user.id,
        "nickname": user.nickname,
        "avatarUrl": _user_avatar_url(user),
        "phone": user.phone,
    }


def _seller_stats(db: Session, seller_id: str) -> tuple[int, int]:
    """(completed trades, positive-rating-rate 0–100) for a seller."""
    trades = (
        db.query(func.count(Order.id))
        .filter(Order.seller_id == seller_id, Order.status == "completed")
        .scalar()
        or 0
    )
    avg_rating = (
        db.query(func.avg(Review.rating))
        .join(Order, Review.order_id == Order.id)
        .filter(Order.seller_id == seller_id, Review.reviewer_id == Order.buyer_id)
        .scalar()
    )
    rating_rate = round(float(avg_rating) / 5.0 * 100) if avg_rating else 100
    return trades, rating_rate


def _admin_bundle_meta(listing: Listing) -> dict | None:
    """Nested bundle meta mirroring the mobile app's BundleMetaDto (admin subset)."""
    if listing.type != "bundle":
        return None
    raw = listing.bundle_meta or {}
    if not isinstance(raw, dict) or not raw:
        return None
    items = []
    for it in raw.get("items") or []:
        status = it.get("status")
        items.append(
            {
                "id": it.get("id"),
                "title": it.get("title", ""),
                "sharePrice": it.get("sharePrice", 0),
                "separatePrice": it.get("separatePrice"),
                "status": status if status in ("available", "onHold", "sold") else "available",
            }
        )
    return {
        "allowSeparateSale": bool(raw.get("allowSeparateSale", False)),
        "pickupWindow": raw.get("pickupWindow"),
        "pickupDeadline": raw.get("pickupDeadline"),
        "items": items,
    }


def _active_keyword_patterns(db: Session) -> list[tuple[str, str]]:
    """(original, lowercased) for every active blocked keyword — load once, reuse per row."""
    return [
        (r.pattern, r.pattern.lower())
        for r in db.query(BlockedKeyword).filter(BlockedKeyword.active.is_(True)).all()
    ]


def _listing_risk(row: Listing, patterns_lower: list[tuple[str, str]] | None) -> tuple[list[str], str]:
    """(matched sensitive words, risk level) for the review UI (敏感词 / 高风险人工审核)."""
    hits: list[str] = []
    if patterns_lower:
        haystack = f"{row.title} {row.description or ''}".lower()
        hits = [orig for orig, low in patterns_lower if low in haystack]
    seller_flagged = bool(row.seller and getattr(row.seller, "is_flagged", False))
    level = "high" if hits or seller_flagged else "normal"
    return hits, level


def _listing_admin_summary(row: Listing, patterns_lower: list[tuple[str, str]] | None = None) -> dict:
    matched, risk = _listing_risk(row, patterns_lower)
    return {
        "id": row.id,
        "type": row.type,
        "title": row.title,
        "price": row.price,
        "reviewStatus": row.review_status,
        "status": row.status,
        "publisher": _party(row.seller),
        "publisherId": row.seller_id,
        "city": row.region_city,
        "area": row.location_label,
        "categoryKey": row.category_key,
        "tagKey": row.tag_key,
        "isRecommended": row.is_recommended,
        "isPinned": row.is_pinned,
        "matchedKeywords": matched,
        "riskLevel": risk,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


def _listing_admin_detail(db: Session, listing: Listing) -> dict:
    seller = listing.seller
    trades, rating_rate = _seller_stats(db, listing.seller_id) if seller else (0, 100)
    updated = getattr(listing, "updated_at", None) or listing.reviewed_at or listing.created_at
    return {
        **_listing_admin_summary(listing, _active_keyword_patterns(db)),
        "description": listing.description,
        "images": normalize_media_urls(listing.images),
        "reviewNote": listing.review_note,
        "publisherPhone": seller.phone if seller else None,
        "publisherCity": seller.city if seller else None,
        "locationLabel": listing.location_label,
        "currency": "AUD",
        "negotiable": listing.negotiable,
        "escrowSupported": listing.escrow_supported,
        "meetInPublic": listing.meet_in_public,
        "pickupMethods": listing.pickup_methods,
        "conditionKey": listing.condition_key,
        "serviceIcon": listing.service_icon if listing.type == "service" else None,
        "merchantPost": bool(getattr(listing, "merchant_post", False)),
        "sellerTrades": trades,
        "sellerRating": rating_rate,
        "sellerVerified": bool(seller.identity_verified or seller.business_verified) if seller else False,
        "viewCount": listing.view_count,
        "favoriteCount": listing.favorite_count or 0,
        "updatedAt": updated.isoformat() if updated else None,
        "bundleMeta": _admin_bundle_meta(listing),
    }


def _get_listing_or_404(db: Session, listing_id: int) -> Listing:
    listing = db.query(Listing).options(joinedload(Listing.seller)).filter(Listing.id == listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Listing not found", "details": {}})
    return listing


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _issue_admin_tokens(db: Session, user: User) -> AuthTokensDto:
    access = create_access_token(user.id)
    refresh = create_refresh_token()
    store_refresh_token(db, user.id, refresh)
    return AuthTokensDto(
        accessToken=access,
        refreshToken=refresh,
        expiresIn=settings.jwt_access_expire_seconds,
        user=user_to_dto(user),
    )


@router.post("/login", response_model=AuthTokensDto)
def admin_login(body: AdminLoginRequest, db: Session = Depends(get_db)):
    phone = normalize_phone(body.phone)
    user = db.query(User).filter(User.phone == phone).first()
    if not user or not user.is_admin or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "Invalid admin credentials", "details": {}})
    return _issue_admin_tokens(db, user)


@router.get("/me")
def admin_me(admin: User = Depends(require_admin)):
    return {
        "id": admin.id,
        "nickname": admin.nickname,
        "phone": admin.phone,
        "isAdmin": bool(admin.is_admin),
    }


def _popular_categories(db: Session, limit: int = 5) -> list[dict]:
    """Top listing categories by active-listing count, joined to their display labels."""
    rows = (
        db.query(Listing.category_key, func.count(Listing.id).label("count"))
        .filter(Listing.status == "active")
        .group_by(Listing.category_key)
        .order_by(func.count(Listing.id).desc())
        .limit(limit)
        .all()
    )
    labels = {c.key: (c.label_en, c.label_zh) for c in db.query(PlatformCategory).all()}
    out = []
    for key, count in rows:
        label_en, label_zh = labels.get(key, (key, key))
        out.append({"key": key, "labelEn": label_en, "labelZh": label_zh, "count": count})
    return out


def _popular_search_terms(db: Session, limit: int = 8) -> list[dict]:
    rows = (
        db.query(SearchLog.term, func.count(SearchLog.id).label("count"))
        .group_by(SearchLog.term)
        .order_by(func.count(SearchLog.id).desc())
        .limit(limit)
        .all()
    )
    return [{"term": term, "count": count} for term, count in rows]


@router.get("/stats")
def dashboard_stats(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    today = datetime.now(timezone.utc).date().isoformat()
    dau_row = db.query(DailyActiveUser).filter(DailyActiveUser.day == today).first()
    return {
        "totalUsers": db.query(func.count(User.id)).scalar() or 0,
        "newUsersToday": db.query(func.count(User.id)).filter(func.date(User.created_at) == today).scalar() or 0,
        "totalListings": db.query(func.count(Listing.id)).scalar() or 0,
        "activeListingCount": db.query(func.count(Listing.id)).filter(Listing.status == "active").scalar() or 0,
        "pendingReviewCount": db.query(func.count(Listing.id)).filter(Listing.review_status == "pendingReview").scalar() or 0,
        "pendingProductCount": db.query(func.count(Listing.id))
        .filter(Listing.review_status == "pendingReview", Listing.type == "product")
        .scalar()
        or 0,
        "reportCount": db.query(func.count(SafetyReport.id)).filter(SafetyReport.status == "pending").scalar() or 0,
        "orderCount": db.query(func.count(Order.id)).scalar() or 0,
        "completedTradeCount": db.query(func.count(Order.id)).filter(Order.status == "completed").scalar() or 0,
        "disputeOrderCount": db.query(func.count(Order.id)).filter(Order.status.in_(("inDispute", "refundInProgress"))).scalar() or 0,
        "dau": dau_row.user_count if dau_row else 0,
        "pendingVerificationCount": db.query(func.count(VerificationSubmission.id))
        .filter(VerificationSubmission.status == "pending")
        .scalar()
        or 0,
        "popularCategories": _popular_categories(db),
        "popularSearchTerms": _popular_search_terms(db),
    }


@router.get("/users")
def list_users(
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    rows = _visible_admin_users(db.query(User).order_by(User.created_at.desc()).all())
    total = len(rows)
    page_rows = rows[(page - 1) * pageSize : (page - 1) * pageSize + pageSize]
    return {
        "items": [
            {
                "id": u.id,
                "nickname": u.nickname,
                "phone": u.phone,
                "city": u.city,
                "avatarUrl": _user_avatar_url(u),
                "identityVerified": u.identity_verified,
                "accountStatus": u.account_status,
                "createdAt": u.created_at.isoformat() if u.created_at else None,
            }
            for u in page_rows
        ],
        "total": total,
        "page": page,
        "pageSize": pageSize,
    }


@router.post("/users/{user_id}/ban")
def ban_user(user_id: str, body: RejectRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "User not found", "details": {}})
    user.account_status = "banned"
    user.banned_at = datetime.now(timezone.utc)
    user.ban_reason = body.reason
    log_admin_action(db, admin_id=admin.id, action_type="ban_user", target_type="user", target_id=user_id, after={"reason": body.reason})
    db.commit()
    return {"ok": True}


@router.post("/users/{user_id}/unban")
def unban_user(user_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "User not found", "details": {}})
    user.account_status = "normal"
    user.banned_at = None
    user.ban_reason = None
    log_admin_action(db, admin_id=admin.id, action_type="unban_user", target_type="user", target_id=user_id)
    db.commit()
    return {"ok": True}


@router.patch("/users/{user_id}/notes")
def set_user_notes(user_id: str, body: AdminNoteRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "User not found", "details": {}})
    user.admin_notes = body.note
    db.commit()
    return {"ok": True}


@router.get("/users/{user_id}")
def get_user(user_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "User not found", "details": {}})
    listing_count = db.query(Listing).filter(Listing.seller_id == user.id).count()
    order_count = db.query(Order).filter(or_(Order.buyer_id == user.id, Order.seller_id == user.id)).count()
    return {
        "id": user.id,
        "nickname": user.nickname,
        "phone": user.phone,
        "city": user.city,
        "avatarUrl": _user_avatar_url(user),
        "identityVerified": user.identity_verified,
        "businessVerified": user.business_verified,
        "accountStatus": user.account_status,
        "banReason": user.ban_reason,
        "adminNotes": user.admin_notes,
        "isMuted": bool(user.is_muted),
        "muteReason": user.mute_reason,
        "publishRestricted": bool(user.publish_restricted),
        "publishRestrictReason": user.publish_restrict_reason,
        "isFlagged": bool(user.is_flagged),
        "flagReason": user.flag_reason,
        "createdAt": user.created_at.isoformat() if user.created_at else None,
        "listingCount": listing_count,
        "orderCount": order_count,
    }


def _get_user_or_404(db: Session, user_id: str) -> User:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "User not found", "details": {}})
    return user


@router.post("/users/{user_id}/mute")
def mute_user(user_id: str, body: UserModerateRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    user = _get_user_or_404(db, user_id)
    user.is_muted = True
    user.muted_at = datetime.now(timezone.utc)
    user.mute_reason = body.reason or None
    log_admin_action(db, admin_id=admin.id, action_type="mute_user", target_type="user", target_id=user_id, after={"reason": body.reason})
    db.commit()
    return {"ok": True}


@router.post("/users/{user_id}/unmute")
def unmute_user(user_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    user = _get_user_or_404(db, user_id)
    user.is_muted = False
    user.muted_at = None
    user.mute_reason = None
    log_admin_action(db, admin_id=admin.id, action_type="unmute_user", target_type="user", target_id=user_id)
    db.commit()
    return {"ok": True}


@router.post("/users/{user_id}/restrict-publish")
def restrict_publish(user_id: str, body: UserModerateRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    user = _get_user_or_404(db, user_id)
    user.publish_restricted = True
    user.publish_restricted_at = datetime.now(timezone.utc)
    user.publish_restrict_reason = body.reason or None
    log_admin_action(db, admin_id=admin.id, action_type="restrict_publish", target_type="user", target_id=user_id, after={"reason": body.reason})
    db.commit()
    return {"ok": True}


@router.post("/users/{user_id}/unrestrict-publish")
def unrestrict_publish(user_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    user = _get_user_or_404(db, user_id)
    user.publish_restricted = False
    user.publish_restricted_at = None
    user.publish_restrict_reason = None
    log_admin_action(db, admin_id=admin.id, action_type="unrestrict_publish", target_type="user", target_id=user_id)
    db.commit()
    return {"ok": True}


@router.post("/users/{user_id}/flag")
def flag_user(user_id: str, body: UserModerateRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    user = _get_user_or_404(db, user_id)
    user.is_flagged = True
    user.flag_reason = body.reason or None
    log_admin_action(db, admin_id=admin.id, action_type="flag_user", target_type="user", target_id=user_id, after={"reason": body.reason})
    db.commit()
    return {"ok": True}


@router.post("/users/{user_id}/unflag")
def unflag_user(user_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    user = _get_user_or_404(db, user_id)
    user.is_flagged = False
    user.flag_reason = None
    log_admin_action(db, admin_id=admin.id, action_type="unflag_user", target_type="user", target_id=user_id)
    db.commit()
    return {"ok": True}


@router.get("/users/{user_id}/listings")
def get_user_listings(user_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    rows = (
        db.query(Listing)
        .options(joinedload(Listing.seller))
        .filter(Listing.seller_id == user_id)
        .order_by(Listing.created_at.desc())
        .limit(100)
        .all()
    )
    return {"items": [_listing_admin_summary(row) for row in rows]}


@router.get("/users/{user_id}/orders")
def get_user_orders(user_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    rows = (
        db.query(Order)
        .options(joinedload(Order.listing), joinedload(Order.buyer), joinedload(Order.seller))
        .filter(or_(Order.buyer_id == user_id, Order.seller_id == user_id))
        .order_by(Order.created_at.desc())
        .limit(100)
        .all()
    )
    return {
        "items": [
            {
                **_order_admin_summary(row),
                "role": "buyer" if row.buyer_id == user_id else "seller",
            }
            for row in rows
        ]
    }


@router.get("/content")
def list_content(
    reviewStatus: str | None = None,
    contentType: str | None = None,
    riskLevel: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    q = db.query(Listing).options(joinedload(Listing.seller)).order_by(Listing.created_at.desc())
    if reviewStatus:
        q = q.filter(Listing.review_status == reviewStatus)
    if contentType:
        q = q.filter(Listing.type == contentType)
    if search and search.strip():
        # 商品列表 title search for the catalog-management view.
        q = q.filter(Listing.title.ilike(f"%{search.strip()}%"))
    patterns = _active_keyword_patterns(db)
    if riskLevel == "high":
        # High-risk manual review (高风险人工审核): sensitive-word hit OR flagged seller.
        # Evaluated in Python so keyword matching and the seller flag share one definition.
        rows_all = q.all()
        filtered = [r for r in rows_all if _listing_risk(r, patterns)[1] == "high"]
        total = len(filtered)
        rows = filtered[(page - 1) * pageSize : (page - 1) * pageSize + pageSize]
    else:
        total = q.count()
        rows = q.offset((page - 1) * pageSize).limit(pageSize).all()
    return {
        "items": [_listing_admin_summary(row, patterns) for row in rows],
        "total": total,
        "page": page,
        "pageSize": pageSize,
    }


@router.get("/content/{listing_id}")
def get_content_detail(listing_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    return _listing_admin_detail(db, _get_listing_or_404(db, listing_id))


@router.post("/content/{listing_id}/approve")
def approve_content(listing_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Listing not found", "details": {}})
    listing.review_status = "approved"
    listing.reviewed_at = datetime.now(timezone.utc)
    listing.reviewed_by = admin.id
    if listing.status == "draft":
        listing.status = "active"
    log_admin_action(db, admin_id=admin.id, action_type="approve_content", target_type="listing", target_id=listing_id)
    db.commit()
    return {"ok": True}


@router.post("/content/{listing_id}/reject")
def reject_content(listing_id: int, body: RejectRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Listing not found", "details": {}})
    listing.review_status = "rejected"
    listing.review_note = body.reason
    listing.reviewed_at = datetime.now(timezone.utc)
    listing.reviewed_by = admin.id
    log_admin_action(db, admin_id=admin.id, action_type="reject_content", target_type="listing", target_id=listing_id, after={"reason": body.reason})
    db.commit()
    return {"ok": True}


@router.patch("/content/{listing_id}")
def edit_content(listing_id: int, body: ContentEditRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Listing not found", "details": {}})
    before = {"title": listing.title, "description": listing.description, "categoryKey": listing.category_key}
    if body.title is not None:
        listing.title = body.title
    if body.description is not None:
        listing.description = body.description
    if body.categoryKey is not None:
        listing.category_key = body.categoryKey
    after = {"title": listing.title, "description": listing.description, "categoryKey": listing.category_key}
    log_admin_action(db, admin_id=admin.id, action_type="edit_content", target_type="listing", target_id=listing_id, before=before, after=after)
    db.commit()
    return {"ok": True}


@router.post("/content/{listing_id}/remove")
def remove_content(
    listing_id: int,
    body: AdminNoteRequest | None = None,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    listing = _get_listing_or_404(db, listing_id)
    listing.review_status = "removed"
    listing.status = "inactive"
    if body and body.note:
        listing.review_note = body.note
    listing.reviewed_at = datetime.now(timezone.utc)
    listing.reviewed_by = admin.id
    log_admin_action(db, admin_id=admin.id, action_type="remove_content", target_type="listing", target_id=listing_id)
    db.commit()
    return {"ok": True}


@router.post("/content/{listing_id}/restore")
def restore_content(listing_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    listing = _get_listing_or_404(db, listing_id)
    listing.review_status = "approved"
    listing.status = "active"
    listing.reviewed_at = datetime.now(timezone.utc)
    listing.reviewed_by = admin.id
    log_admin_action(db, admin_id=admin.id, action_type="restore_content", target_type="listing", target_id=listing_id)
    db.commit()
    return {"ok": True}


@router.patch("/content/{listing_id}/flags")
def set_content_flags(listing_id: int, body: ContentFlagsRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    listing = _get_listing_or_404(db, listing_id)
    if body.recommended is not None:
        listing.is_recommended = body.recommended
    if body.pinned is not None:
        listing.is_pinned = body.pinned
    log_admin_action(db, admin_id=admin.id, action_type="set_content_flags", target_type="listing", target_id=listing_id, after=body.model_dump())
    db.commit()
    return {"ok": True}


@router.patch("/content/{listing_id}/note")
def set_content_note(listing_id: int, body: ContentNoteRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    listing = _get_listing_or_404(db, listing_id)
    listing.review_note = body.note
    db.commit()
    return {"ok": True}


@router.patch("/content/{listing_id}/tags")
def set_content_tags(listing_id: int, body: ContentTagsRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    listing = _get_listing_or_404(db, listing_id)
    before = {"tagKey": listing.tag_key}
    listing.tag_key = body.tagKey
    log_admin_action(db, admin_id=admin.id, action_type="set_content_tags", target_type="listing", target_id=listing_id, before=before, after={"tagKey": body.tagKey})
    db.commit()
    return {"ok": True}


@router.get("/content/{listing_id}/reports")
def get_content_reports(listing_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """举报记录: reports filed against this listing so moderators see its history in-context."""
    rows = (
        db.query(SafetyReport, User)
        .join(User, SafetyReport.reporter_id == User.id)
        .filter(
            SafetyReport.target_type.in_(("listing", "service")),
            SafetyReport.target_id == str(listing_id),
        )
        .order_by(SafetyReport.created_at.desc())
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "reason": r.reason,
                "details": r.details,
                "status": r.status,
                "reporter": _party(reporter),
                "createdAt": r.created_at.isoformat() if r.created_at else None,
            }
            for r, reporter in rows
        ]
    }


@router.delete("/content/{listing_id}")
def delete_content(listing_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """删除违规商品: hard-delete when no orders reference it, else soft-remove to keep FK integrity."""
    listing = _get_listing_or_404(db, listing_id)
    has_orders = db.query(Order.id).filter(Order.listing_id == listing_id).first() is not None
    log_admin_action(db, admin_id=admin.id, action_type="delete_content", target_type="listing", target_id=listing_id, before=_listing_admin_summary(listing))
    if has_orders:
        # Cannot drop the row (orders/reviews depend on it) — take it down permanently instead.
        listing.review_status = "removed"
        listing.status = "deleted"
        listing.reviewed_at = datetime.now(timezone.utc)
        listing.reviewed_by = admin.id
        db.commit()
        return {"ok": True, "deleted": False}
    db.query(Favorite).filter(Favorite.listing_id == listing_id).delete(synchronize_session=False)
    db.query(ViewHistory).filter(ViewHistory.listing_id == listing_id).delete(synchronize_session=False)
    db.delete(listing)
    db.commit()
    return {"ok": True, "deleted": True}


@router.get("/verifications")
def list_verifications(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    rows = (
        db.query(VerificationSubmission)
        .options(joinedload(VerificationSubmission.user))
        .order_by(VerificationSubmission.created_at.desc())
        .limit(100)
        .all()
    )
    visible_rows: list[VerificationSubmission] = []
    seen: set[str] = set()
    for row in rows:
        if not _is_visible_admin_user(row.user):
            continue
        key = _visible_admin_nickname(row.user.nickname)
        if not key or key in seen:
            continue
        visible_rows.append(row)
        seen.add(key)
    return {
        "items": [
            {
                "id": row.id,
                "userId": row.user_id,
                "nickname": row.user.nickname if row.user else None,
                "phone": row.user.phone if row.user else None,
                "avatarUrl": _user_avatar_url(row.user) if row.user else None,
                "status": row.status,
                "legalName": row.legal_name,
                "createdAt": row.created_at.isoformat() if row.created_at else None,
            }
            for row in visible_rows
        ]
    }


@router.get("/verifications/{submission_id}")
def get_verification_detail(submission_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    sub = (
        db.query(VerificationSubmission)
        .options(joinedload(VerificationSubmission.user))
        .filter(VerificationSubmission.id == submission_id)
        .first()
    )
    if not sub:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Submission not found", "details": {}})
    return {
        "id": sub.id,
        "userId": sub.user_id,
        "nickname": sub.user.nickname if sub.user else None,
        "phone": sub.user.phone if sub.user else None,
        "avatarUrl": _user_avatar_url(sub.user) if sub.user else None,
        "status": sub.status,
        "legalName": sub.legal_name,
        "idCountry": sub.id_country,
        "idFrontUrl": sub.id_front_url,
        "idBackUrl": sub.id_back_url,
        "businessName": sub.business_name,
        "businessRegUrl": sub.business_reg_url,
        "abn": sub.abn,
        "rejectionReason": sub.rejection_reason,
        "createdAt": sub.created_at.isoformat() if sub.created_at else None,
        "reviewedAt": sub.reviewed_at.isoformat() if sub.reviewed_at else None,
    }


@router.post("/verifications/{submission_id}/approve")
def approve_verification(submission_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    sub = db.query(VerificationSubmission).filter(VerificationSubmission.id == submission_id).first()
    if not sub:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Submission not found", "details": {}})
    sub.status = "approved"
    sub.reviewed_at = datetime.now(timezone.utc)
    sub.reviewed_by = admin.id
    user = db.query(User).filter(User.id == sub.user_id).first()
    if user:
        user.identity_verified = True
        if sub.business_name or sub.abn or sub.business_reg_url:
            user.business_verified = True
    log_admin_action(db, admin_id=admin.id, action_type="approve_verification", target_type="verification", target_id=submission_id)
    db.commit()
    return {"ok": True}


@router.post("/verifications/{submission_id}/reject")
def reject_verification(submission_id: str, body: RejectRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    sub = db.query(VerificationSubmission).filter(VerificationSubmission.id == submission_id).first()
    if not sub:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Submission not found", "details": {}})
    sub.status = "rejected"
    sub.rejection_reason = body.reason
    sub.reviewed_at = datetime.now(timezone.utc)
    sub.reviewed_by = admin.id
    # Rejecting revokes any verification the submission would have granted, so a
    # previously-approved user who is re-reviewed and rejected loses the flags.
    user = db.query(User).filter(User.id == sub.user_id).first()
    if user:
        user.identity_verified = False
        if sub.business_name or sub.abn or sub.business_reg_url:
            user.business_verified = False
    log_admin_action(db, admin_id=admin.id, action_type="reject_verification", target_type="verification", target_id=submission_id, after={"reason": body.reason})
    db.commit()
    return {"ok": True}


def _review_summary(review: Review, order: Order | None, reviewer: User | None) -> dict:
    listing = order.listing if order else None
    return {
        "id": review.id,
        "orderId": review.order_id,
        "rating": review.rating,
        "comment": review.comment,
        "isHidden": bool(review.is_hidden),
        "isRemoved": bool(review.is_removed),
        "reviewer": _party(reviewer),
        "listingId": listing.id if listing else None,
        "listingTitle": listing.title if listing else None,
        "createdAt": review.created_at.isoformat() if review.created_at else None,
    }


@router.get("/reviews")
def list_reviews(
    filter: str = Query("all", pattern="^(all|visible|hidden|removed)$"),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    q = (
        db.query(Review, Order, User)
        .outerjoin(Order, Review.order_id == Order.id)
        .outerjoin(User, Review.reviewer_id == User.id)
        .options(joinedload(Order.listing))
        .order_by(Review.created_at.desc())
    )
    if filter == "visible":
        q = q.filter(Review.is_hidden.is_(False), Review.is_removed.is_(False))
    elif filter == "hidden":
        q = q.filter(Review.is_hidden.is_(True))
    elif filter == "removed":
        q = q.filter(Review.is_removed.is_(True))
    rows = q.limit(200).all()
    return {"items": [_review_summary(rv, order, reviewer) for rv, order, reviewer in rows]}


@router.get("/reviews/{review_id}")
def get_review_detail(review_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    review = db.query(Review).filter(Review.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Review not found", "details": {}})
    order = db.query(Order).options(joinedload(Order.listing)).filter(Order.id == review.order_id).first()
    reviewer = db.query(User).filter(User.id == review.reviewer_id).first()
    reviewee = None
    if order:
        reviewee_id = order.seller_id if review.reviewer_id == order.buyer_id else order.buyer_id
        reviewee = db.query(User).filter(User.id == reviewee_id).first()
    listing = order.listing if order else None
    return {
        **_review_summary(review, order, reviewer),
        "adminNote": review.admin_note,
        "reviewee": _party(reviewee),
        "listing": _listing_admin_summary(listing) if listing else None,
        "qualityRating": review.quality_rating,
        "communicationRating": review.communication_rating,
        "expertiseRating": review.expertise_rating,
        "professionalismRating": review.professionalism_rating,
        "hireAgainRating": review.hire_again_rating,
    }


@router.post("/reviews/{review_id}/hide")
def hide_review(review_id: str, body: ReviewModerateRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    review = db.query(Review).filter(Review.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Review not found", "details": {}})
    review.is_hidden = True
    if body.note:
        review.admin_note = body.note
    log_admin_action(db, admin_id=admin.id, action_type="hide_review", target_type="review", target_id=review_id, after={"note": body.note})
    db.commit()
    return {"ok": True}


@router.post("/reviews/{review_id}/unhide")
def unhide_review(review_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    review = db.query(Review).filter(Review.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Review not found", "details": {}})
    review.is_hidden = False
    log_admin_action(db, admin_id=admin.id, action_type="unhide_review", target_type="review", target_id=review_id)
    db.commit()
    return {"ok": True}


@router.delete("/reviews/{review_id}")
def delete_review(review_id: str, body: ReviewModerateRequest | None = None, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """删除违规评价: soft-delete so it disappears publicly but stays auditable."""
    review = db.query(Review).filter(Review.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Review not found", "details": {}})
    review.is_removed = True
    if body and body.note:
        review.admin_note = body.note
    log_admin_action(db, admin_id=admin.id, action_type="delete_review", target_type="review", target_id=review_id)
    db.commit()
    return {"ok": True}


@router.get("/reports")
def list_reports(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    rows = (
        db.query(SafetyReport, User)
        .join(User, SafetyReport.reporter_id == User.id)
        .order_by(SafetyReport.created_at.desc())
        .limit(100)
        .all()
    )
    return {
        "items": [
            {
                "id": row.id,
                "targetType": row.target_type,
                "targetId": row.target_id,
                "reason": row.reason,
                "status": row.status,
                "reporter": _party(reporter),
                "reporterId": row.reporter_id,
                "createdAt": row.created_at.isoformat() if row.created_at else None,
            }
            for row, reporter in rows
        ]
    }


@router.get("/reports/{report_id}")
def get_report_detail(report_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    report = db.query(SafetyReport).filter(SafetyReport.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Report not found", "details": {}})
    reporter = db.query(User).filter(User.id == report.reporter_id).first()
    target_summary = None
    reported_user = None
    if report.target_type in ("listing", "service") and report.target_id.isdigit():
        listing = db.query(Listing).options(joinedload(Listing.seller)).filter(Listing.id == int(report.target_id)).first()
        if listing:
            target_summary = _listing_admin_summary(listing)
            reported_user = listing.seller
    elif report.target_type == "user":
        reported_user = db.query(User).filter(User.id == report.target_id).first()
    elif report.target_type == "order" and report.target_id.isdigit():
        order = db.query(Order).options(joinedload(Order.listing)).filter(Order.id == int(report.target_id)).first()
        if order and order.listing:
            target_summary = {"orderId": order.id, "title": order.listing.title, "status": order.status}
    return {
        "id": report.id,
        "targetType": report.target_type,
        "targetId": report.target_id,
        "reason": report.reason,
        "details": report.details,
        "evidenceUrls": report.evidence_urls or [],
        "status": report.status,
        "handlerNote": report.handler_note,
        "reporter": _party(reporter)
        or {"id": report.reporter_id, "nickname": None, "avatarUrl": None, "phone": None},
        "reportedUser": _party(reported_user),
        "targetSummary": target_summary,
        "createdAt": report.created_at.isoformat() if report.created_at else None,
    }


@router.post("/reports/{report_id}/action")
def handle_report(
    report_id: str,
    body: ReportActionRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    report = db.query(SafetyReport).filter(SafetyReport.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Report not found", "details": {}})
    report.handler_note = body.note or report.handler_note
    report.handled_by = admin.id
    report.handled_at = datetime.now(timezone.utc)

    if body.action == "ignore":
        report.status = "ignored"
    elif body.action == "warn":
        report.status = "processed"
    elif body.action == "remove_content" and report.target_type in ("listing", "service") and report.target_id.isdigit():
        listing = _get_listing_or_404(db, int(report.target_id))
        listing.review_status = "removed"
        listing.status = "inactive"
        report.status = "processed"
    elif body.action == "ban_user":
        user_id = report.target_id if report.target_type == "user" else None
        if not user_id and report.target_type in ("listing", "service") and report.target_id.isdigit():
            listing = db.query(Listing).filter(Listing.id == int(report.target_id)).first()
            user_id = listing.seller_id if listing else None
        if user_id:
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                user.account_status = "banned"
                user.banned_at = datetime.now(timezone.utc)
                user.ban_reason = body.note or report.reason
        report.status = "processed"
    elif body.action == "restore_content" and report.target_type in ("listing", "service") and report.target_id.isdigit():
        listing = _get_listing_or_404(db, int(report.target_id))
        listing.review_status = "approved"
        listing.status = "active"
        report.status = "processed"
    else:
        report.status = "processed"

    log_admin_action(
        db,
        admin_id=admin.id,
        action_type=f"report_{body.action}",
        target_type="report",
        target_id=report_id,
        after={"note": body.note},
    )
    db.commit()
    return {"ok": True}


@router.get("/reports/{report_id}/chat-transcript")
def report_chat_transcript(report_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    report = db.query(SafetyReport).filter(SafetyReport.id == report_id).first()
    if not report or report.target_type != "chat":
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Chat report not found", "details": {}})
    conv = db.query(Conversation).filter(Conversation.id == report.target_id).first()
    if not conv:
        return {"messages": []}
    messages = db.query(Message).filter(Message.conversation_id == conv.id).order_by(Message.sent_at.asc()).all()
    return {
        "messages": [
            {"senderId": m.sender_id, "text": m.text, "sentAt": m.sent_at.isoformat() if m.sent_at else None}
            for m in messages
        ]
    }


@router.get("/orders")
def list_admin_orders(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    rows = db.query(Order).options(joinedload(Order.listing), joinedload(Order.buyer), joinedload(Order.seller)).order_by(Order.created_at.desc()).limit(100).all()
    return {
        "items": [_order_admin_summary(row) for row in rows],
    }


@router.post("/orders/{order_id}/pause-payout")
def pause_payout(order_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Order not found", "details": {}})
    order.payout_paused = True
    if getattr(order, "payout_status", None) not in ("released", "reversed"):
        order.payout_status = "blocked"
        order.payout_failure_code = "PAYOUT_PAUSED"
        order.payout_failure_reason = "Payout was paused by an administrator"
    log_admin_action(db, admin_id=admin.id, action_type="pause_payout", target_type="order", target_id=order_id)
    db.commit()
    return {"ok": True}


@router.post("/orders/{order_id}/release-payout")
def release_payout(order_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    order = _get_order_or_404(db, order_id)
    transition = release_payout_for_order(db, order)
    log_admin_action(
        db,
        admin_id=admin.id,
        action_type="release_payout",
        target_type="order",
        target_id=order_id,
        after={"status": transition.status, "code": transition.code, "reference": transition.reference},
    )
    db.commit()
    return {"ok": True, "status": transition.status, "reference": transition.reference}


def _order_admin_summary(row: Order) -> dict:
    return {
        "id": row.id,
        "buyer": _party(row.buyer),
        "buyerId": row.buyer_id,
        "seller": _party(row.seller),
        "sellerId": row.seller_id,
        "title": row.listing.title if row.listing else None,
        "listingId": row.listing_id,
        "amount": row.amount,
        "escrowFee": row.escrow_fee,
        "status": row.status,
        "paymentStatus": row.payment_status,
        "paymentMethod": row.payment_method,
        "psp": row.psp,
        "pspTransactionId": row.psp_transaction_id,
        "pspPaymentId": row.psp_payment_id,
        "payoutPaused": row.payout_paused,
        "payoutStatus": getattr(row, "payout_status", None),
        "payoutProvider": getattr(row, "payout_provider", None),
        "payoutMethodId": getattr(row, "payout_method_id", None),
        "payoutReference": getattr(row, "payout_reference", None),
        "payoutFailureCode": getattr(row, "payout_failure_code", None),
        "payoutFailureReason": getattr(row, "payout_failure_reason", None),
        "payoutReleasedAt": row.payout_released_at.isoformat() if getattr(row, "payout_released_at", None) else None,
        "payoutFailedAt": row.payout_failed_at.isoformat() if getattr(row, "payout_failed_at", None) else None,
        "payoutReversedAt": row.payout_reversed_at.isoformat() if getattr(row, "payout_reversed_at", None) else None,
        "payoutReversalReference": getattr(row, "payout_reversal_reference", None),
        "isAbnormal": row.is_abnormal,
        "adminNotes": row.admin_notes,
        "disputeStatus": row.dispute_status,
        "disputeReason": row.dispute_reason,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


def _get_order_or_404(db: Session, order_id: int) -> Order:
    order = (
        db.query(Order)
        .options(joinedload(Order.listing), joinedload(Order.buyer), joinedload(Order.seller))
        .filter(Order.id == order_id)
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Order not found", "details": {}})
    return order


@router.get("/orders/{order_id}")
def get_order_detail(order_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    return _order_admin_summary(_get_order_or_404(db, order_id))


@router.get("/orders/{order_id}/chat")
def get_order_chat(order_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """Buyer↔seller conversation tied to this order — mirrors the mobile ChatMessageDto shape."""
    order = _get_order_or_404(db, order_id)
    conv = (
        db.query(Conversation)
        .filter(
            Conversation.listing_id == order.listing_id,
            Conversation.buyer_id == order.buyer_id,
            Conversation.seller_id == order.seller_id,
        )
        .first()
    )
    if not conv:
        return {"messages": []}
    messages = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id)
        .order_by(Message.sent_at.asc())
        .all()
    )
    return {
        "messages": [
            {
                "id": m.id,
                "conversationId": m.conversation_id,
                "senderId": m.sender_id,
                "text": m.text,
                "sentAt": m.sent_at.isoformat() if m.sent_at else None,
            }
            for m in messages
        ]
    }


@router.post("/orders/{order_id}/mark-abnormal")
def mark_order_abnormal(order_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    order = _get_order_or_404(db, order_id)
    order.is_abnormal = True
    log_admin_action(db, admin_id=admin.id, action_type="mark_abnormal", target_type="order", target_id=order_id)
    db.commit()
    return {"ok": True}


@router.patch("/orders/{order_id}/notes")
def set_order_notes(order_id: int, body: AdminNoteRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    order = _get_order_or_404(db, order_id)
    order.admin_notes = body.note
    db.commit()
    return {"ok": True}


@router.post("/orders/{order_id}/resolve-dispute")
def resolve_dispute(
    order_id: int,
    body: DisputeResolveRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    order = _get_order_or_404(db, order_id)
    if order.status not in ("inDispute", "refundInProgress"):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_STATE", "message": "Order is not in dispute", "details": {}},
        )
    order.payout_paused = True
    order.admin_notes = body.note or order.admin_notes
    order.dispute_status = "resolved"
    if body.resolution == "refund":
        payout_transition = reverse_released_payout_for_order(order)
        if getattr(order, "payout_status", None) == "released" and payout_transition.status != "reversed":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "PAYOUT_REVERSAL_REQUIRED",
                    "message": payout_transition.code or "Seller payout could not be reversed",
                    "details": {"reason": payout_transition.reference or payout_transition.status},
                },
            )
        refund_transition = refund_order_payment(order)
        if refund_transition.status != "refunded":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": refund_transition.code or "REFUND_FAILED",
                    "message": refund_transition.message or "Buyer payment could not be refunded",
                    "details": {},
                },
            )
        order.status = "refunded"
        order.payout_paused = False
    elif body.resolution == "complete":
        order.status = "completed"
        order.payout_paused = False
        release_payout_for_order(db, order)
    order.updated_at = datetime.now(timezone.utc)
    log_admin_action(
        db,
        admin_id=admin.id,
        action_type="resolve_dispute",
        target_type="order",
        target_id=order_id,
        after={"resolution": body.resolution, "note": body.note},
    )
    db.commit()
    return {"ok": True}


@router.get("/config/categories")
def list_categories(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    rows = db.query(PlatformCategory).order_by(PlatformCategory.sort_order.asc(), PlatformCategory.id.asc()).all()
    return {
        "items": [
            {
                "id": row.id,
                "type": row.type,
                "key": row.key,
                "labelEn": row.label_en,
                "labelZh": row.label_zh,
                "sortOrder": row.sort_order,
                "enabled": row.enabled,
                "icon": row.icon,
                "showOnHome": bool(row.show_on_home),
            }
            for row in rows
        ]
    }


@router.post("/config/categories")
def create_category(body: CategoryUpsertRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    if db.query(PlatformCategory).filter(PlatformCategory.key == body.key).first():
        raise HTTPException(status_code=409, detail={"code": "CONFLICT", "message": "Category key exists", "details": {}})
    row = PlatformCategory(
        type=body.type,
        key=body.key,
        label_en=body.labelEn,
        label_zh=body.labelZh,
        sort_order=body.sortOrder,
        enabled=body.enabled,
        icon=body.icon,
        show_on_home=body.showOnHome,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id}


@router.patch("/config/categories/{category_id}")
def patch_category(category_id: int, body: CategoryPatchRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    row = db.query(PlatformCategory).filter(PlatformCategory.id == category_id).first()
    if not row:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Category not found", "details": {}})
    if body.labelEn is not None:
        row.label_en = body.labelEn
    if body.labelZh is not None:
        row.label_zh = body.labelZh
    if body.sortOrder is not None:
        row.sort_order = body.sortOrder
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.icon is not None:
        row.icon = body.icon
    if body.showOnHome is not None:
        row.show_on_home = body.showOnHome
    db.commit()
    return {"ok": True}


@router.get("/config/regions")
def list_regions(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    rows = db.query(PlatformRegion).order_by(PlatformRegion.sort_order.asc(), PlatformRegion.id.asc()).all()
    return {
        "items": [
            {
                "id": row.id,
                "country": row.country,
                "state": row.state,
                "city": row.city,
                "area": row.area,
                "labelEn": row.label_en,
                "labelZh": row.label_zh,
                "isDefaultCity": row.is_default_city,
                "sortOrder": row.sort_order,
                "enabled": row.enabled,
            }
            for row in rows
        ]
    }


@router.post("/config/regions")
def create_region(body: RegionUpsertRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    if body.isDefaultCity:
        db.query(PlatformRegion).filter(PlatformRegion.is_default_city.is_(True)).update({"is_default_city": False})
    row = PlatformRegion(
        country=body.country.upper(),
        state=body.state,
        city=body.city,
        area=body.area,
        label_en=body.labelEn,
        label_zh=body.labelZh,
        is_default_city=body.isDefaultCity,
        sort_order=body.sortOrder,
        enabled=body.enabled,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id}


@router.patch("/config/regions/{region_id}")
def patch_region(region_id: int, body: RegionPatchRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    row = db.query(PlatformRegion).filter(PlatformRegion.id == region_id).first()
    if not row:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Region not found", "details": {}})
    if body.isDefaultCity is True:
        db.query(PlatformRegion).filter(PlatformRegion.is_default_city.is_(True)).update({"is_default_city": False})
        row.is_default_city = True
    elif body.isDefaultCity is False:
        row.is_default_city = False
    if body.labelEn is not None:
        row.label_en = body.labelEn
    if body.labelZh is not None:
        row.label_zh = body.labelZh
    if body.sortOrder is not None:
        row.sort_order = body.sortOrder
    if body.enabled is not None:
        row.enabled = body.enabled
    db.commit()
    return {"ok": True}


@router.get("/config/banners")
def list_banners(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    rows = db.query(PlatformBanner).order_by(PlatformBanner.created_at.desc()).all()
    return {
        "items": [
            {
                "id": row.id,
                "title": row.title,
                "imageUrl": row.image_url,
                "linkUrl": row.link_url,
                "position": row.position,
                "onlineAt": row.online_at.isoformat() if row.online_at else None,
                "offlineAt": row.offline_at.isoformat() if row.offline_at else None,
                "enabled": row.enabled,
            }
            for row in rows
        ]
    }


@router.post("/config/banners")
def create_banner(body: BannerUpsertRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    row = PlatformBanner(
        title=body.title,
        image_url=body.imageUrl,
        link_url=body.linkUrl,
        position=body.position,
        online_at=_parse_iso_dt(body.onlineAt),
        offline_at=_parse_iso_dt(body.offlineAt),
        enabled=body.enabled,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id}


@router.patch("/config/banners/{banner_id}")
def patch_banner(banner_id: str, body: BannerPatchRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    row = db.query(PlatformBanner).filter(PlatformBanner.id == banner_id).first()
    if not row:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Banner not found", "details": {}})
    if body.title is not None:
        row.title = body.title
    if body.imageUrl is not None:
        row.image_url = body.imageUrl
    if body.linkUrl is not None:
        row.link_url = body.linkUrl
    if body.position is not None:
        row.position = body.position
    if body.onlineAt is not None:
        row.online_at = _parse_iso_dt(body.onlineAt)
    if body.offlineAt is not None:
        row.offline_at = _parse_iso_dt(body.offlineAt)
    if body.enabled is not None:
        row.enabled = body.enabled
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# System config — 禁发关键词 / 举报原因 / 商品标签 / 首页开关 & 协议 (points #10, #11)
# ---------------------------------------------------------------------------


@router.get("/config/keywords")
def list_keywords(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    rows = db.query(BlockedKeyword).order_by(BlockedKeyword.id.asc()).all()
    return {
        "items": [
            {"id": r.id, "pattern": r.pattern, "locale": r.locale, "active": r.active}
            for r in rows
        ]
    }


@router.post("/config/keywords")
def create_keyword(body: KeywordUpsertRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    if db.query(BlockedKeyword).filter(BlockedKeyword.pattern == body.pattern).first():
        raise HTTPException(status_code=409, detail={"code": "CONFLICT", "message": "Keyword exists", "details": {}})
    row = BlockedKeyword(pattern=body.pattern, locale=body.locale, active=body.active)
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id}


@router.patch("/config/keywords/{keyword_id}")
def patch_keyword(keyword_id: int, body: KeywordPatchRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    row = db.query(BlockedKeyword).filter(BlockedKeyword.id == keyword_id).first()
    if not row:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Keyword not found", "details": {}})
    if body.pattern is not None:
        row.pattern = body.pattern
    if body.locale is not None:
        row.locale = body.locale
    if body.active is not None:
        row.active = body.active
    db.commit()
    return {"ok": True}


@router.delete("/config/keywords/{keyword_id}")
def delete_keyword(keyword_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    row = db.query(BlockedKeyword).filter(BlockedKeyword.id == keyword_id).first()
    if not row:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Keyword not found", "details": {}})
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.get("/config/report-reasons")
def list_report_reasons(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    rows = db.query(ReportReason).order_by(ReportReason.sort_order.asc(), ReportReason.id.asc()).all()
    return {
        "items": [
            {"id": r.id, "key": r.key, "labelEn": r.label_en, "labelZh": r.label_zh, "sortOrder": r.sort_order, "active": r.active}
            for r in rows
        ]
    }


@router.post("/config/report-reasons")
def create_report_reason(body: ReportReasonUpsertRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    if db.query(ReportReason).filter(ReportReason.key == body.key).first():
        raise HTTPException(status_code=409, detail={"code": "CONFLICT", "message": "Reason key exists", "details": {}})
    row = ReportReason(key=body.key, label_en=body.labelEn, label_zh=body.labelZh, sort_order=body.sortOrder, active=body.active)
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id}


@router.patch("/config/report-reasons/{reason_id}")
def patch_report_reason(reason_id: int, body: ReportReasonPatchRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    row = db.query(ReportReason).filter(ReportReason.id == reason_id).first()
    if not row:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Reason not found", "details": {}})
    if body.labelEn is not None:
        row.label_en = body.labelEn
    if body.labelZh is not None:
        row.label_zh = body.labelZh
    if body.sortOrder is not None:
        row.sort_order = body.sortOrder
    if body.active is not None:
        row.active = body.active
    db.commit()
    return {"ok": True}


@router.delete("/config/report-reasons/{reason_id}")
def delete_report_reason(reason_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    row = db.query(ReportReason).filter(ReportReason.id == reason_id).first()
    if not row:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Reason not found", "details": {}})
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.get("/config/tags")
def list_product_tags(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    rows = db.query(ProductTag).order_by(ProductTag.sort_order.asc(), ProductTag.id.asc()).all()
    return {
        "items": [
            {"id": r.id, "key": r.key, "labelEn": r.label_en, "labelZh": r.label_zh, "sortOrder": r.sort_order, "active": r.active}
            for r in rows
        ]
    }


@router.post("/config/tags")
def create_product_tag(body: ProductTagUpsertRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    if db.query(ProductTag).filter(ProductTag.key == body.key).first():
        raise HTTPException(status_code=409, detail={"code": "CONFLICT", "message": "Tag key exists", "details": {}})
    row = ProductTag(key=body.key, label_en=body.labelEn, label_zh=body.labelZh, sort_order=body.sortOrder, active=body.active)
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id}


@router.patch("/config/tags/{tag_id}")
def patch_product_tag(tag_id: int, body: ProductTagPatchRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    row = db.query(ProductTag).filter(ProductTag.id == tag_id).first()
    if not row:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Tag not found", "details": {}})
    if body.labelEn is not None:
        row.label_en = body.labelEn
    if body.labelZh is not None:
        row.label_zh = body.labelZh
    if body.sortOrder is not None:
        row.sort_order = body.sortOrder
    if body.active is not None:
        row.active = body.active
    db.commit()
    return {"ok": True}


@router.delete("/config/tags/{tag_id}")
def delete_product_tag(tag_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    row = db.query(ProductTag).filter(ProductTag.id == tag_id).first()
    if not row:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Tag not found", "details": {}})
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.get("/config/settings")
def get_settings(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """Return all platform settings as a flat key→value map (home switches, ToS, privacy)."""
    rows = db.query(PlatformSetting).all()
    return {"values": {r.key: r.value for r in rows}}


@router.patch("/config/settings")
def patch_settings(body: SettingsPatchRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    for key, value in body.values.items():
        row = db.query(PlatformSetting).filter(PlatformSetting.key == key).first()
        if row:
            row.value = value
        else:
            db.add(PlatformSetting(key=key, value=value))
    log_admin_action(db, admin_id=admin.id, action_type="patch_settings", target_type="settings", target_id="platform", after={"keys": list(body.values.keys())})
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# 专题 (topic zones) — point #5, distinct from banners
# ---------------------------------------------------------------------------


def _topic_dto(row: PlatformTopic) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "titleZh": row.title_zh,
        "subtitle": row.subtitle,
        "coverImageUrl": row.cover_image_url,
        "tagKey": row.tag_key,
        "linkUrl": row.link_url,
        "onlineAt": row.online_at.isoformat() if row.online_at else None,
        "offlineAt": row.offline_at.isoformat() if row.offline_at else None,
        "sortOrder": row.sort_order,
        "enabled": row.enabled,
    }


@router.get("/config/topics")
def list_topics(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    rows = db.query(PlatformTopic).order_by(PlatformTopic.sort_order.asc(), PlatformTopic.id.asc()).all()
    return {"items": [_topic_dto(r) for r in rows]}


@router.post("/config/topics")
def create_topic(body: TopicUpsertRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    row = PlatformTopic(
        title=body.title,
        title_zh=body.titleZh,
        subtitle=body.subtitle,
        cover_image_url=body.coverImageUrl,
        tag_key=body.tagKey,
        link_url=body.linkUrl,
        online_at=_parse_iso_dt(body.onlineAt),
        offline_at=_parse_iso_dt(body.offlineAt),
        sort_order=body.sortOrder,
        enabled=body.enabled,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id}


@router.patch("/config/topics/{topic_id}")
def patch_topic(topic_id: int, body: TopicPatchRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    row = db.query(PlatformTopic).filter(PlatformTopic.id == topic_id).first()
    if not row:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Topic not found", "details": {}})
    if body.title is not None:
        row.title = body.title
    if body.titleZh is not None:
        row.title_zh = body.titleZh
    if body.subtitle is not None:
        row.subtitle = body.subtitle
    if body.coverImageUrl is not None:
        row.cover_image_url = body.coverImageUrl
    if body.tagKey is not None:
        row.tag_key = body.tagKey
    if body.linkUrl is not None:
        row.link_url = body.linkUrl
    if body.onlineAt is not None:
        row.online_at = _parse_iso_dt(body.onlineAt)
    if body.offlineAt is not None:
        row.offline_at = _parse_iso_dt(body.offlineAt)
    if body.sortOrder is not None:
        row.sort_order = body.sortOrder
    if body.enabled is not None:
        row.enabled = body.enabled
    db.commit()
    return {"ok": True}


@router.delete("/config/topics/{topic_id}")
def delete_topic(topic_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    row = db.query(PlatformTopic).filter(PlatformTopic.id == topic_id).first()
    if not row:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Topic not found", "details": {}})
    db.delete(row)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# 认证管理 — phone / email auth status (point #9)
# ---------------------------------------------------------------------------


@router.get("/auth-status")
def list_auth_status(
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    rows = _visible_admin_users(db.query(User).order_by(User.created_at.desc()).all())
    total = len(rows)
    page_rows = rows[(page - 1) * pageSize : (page - 1) * pageSize + pageSize]
    return {
        "items": [
            {
                "id": u.id,
                "nickname": u.nickname,
                "avatarUrl": _user_avatar_url(u),
                "phone": u.phone,
                "phoneVerified": bool(u.phone_verified) and bool(u.phone),
                "email": u.email,
                "emailVerified": bool(getattr(u, "email_verified", False)),
                "identityVerified": bool(u.identity_verified),
                "businessVerified": bool(u.business_verified),
            }
            for u in page_rows
        ],
        "total": total,
        "page": page,
        "pageSize": pageSize,
    }


# ---------------------------------------------------------------------------
# 聊天风控 — automated sensitive-word detection across chat messages (point #10)
# ---------------------------------------------------------------------------


@router.get("/chat-risk/flagged")
def list_flagged_messages(
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Recent chat messages that hit an active blocked keyword (敏感词检测)."""
    patterns = [(p, pl) for p, pl in _active_keyword_patterns(db)]
    if not patterns:
        return {"items": []}
    messages = (
        db.query(Message)
        .options(joinedload(Message.conversation))
        .order_by(Message.sent_at.desc())
        .limit(1000)
        .all()
    )
    senders = {u.id: u for u in db.query(User).all()}
    items: list[dict] = []
    for m in messages:
        text_low = (m.text or "").lower()
        matched = [orig for orig, low in patterns if low in text_low]
        if not matched:
            continue
        sender = senders.get(m.sender_id)
        items.append(
            {
                "id": m.id,
                "conversationId": m.conversation_id,
                "senderId": m.sender_id,
                "sender": _party(sender),
                "senderMuted": bool(sender.is_muted) if sender else False,
                "text": m.text,
                "matched": matched,
                "sentAt": m.sent_at.isoformat() if m.sent_at else None,
            }
        )
        if len(items) >= limit:
            break
    return {"items": items}
