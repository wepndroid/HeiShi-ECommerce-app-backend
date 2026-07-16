from datetime import datetime, timezone
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.auth import get_accept_language, get_current_user
from app.admin_notifications import notify_admin
from app.catalog_helpers import apply_public_listing_visibility_filter, get_or_create_settings
from app.database import get_db
from app.models import Address, DevicePushToken, Follow, Listing, Order, PaymentMethod, PayoutMethod, Review, User, UserSettings, VerificationSubmission, ViewHistory
from app.config import settings
from app import paypal_partner_service
from app.schemas import (
    AddPaymentMethodRequest,
    AddPayoutMethodRequest,
    AddressCreateRequest,
    AddressUpdateRequest,
    AddressDto,
    AuthUserDto,
    BindVerificationRequest,
    CacheClearResponse,
    ConnectOnboardingResponse,
    ConnectStatusResponse,
    CreditProfileDto,
    DataExportDto,
    NotificationSettingsDto,
    PaymentMethodDto,
    PayoutMethodDto,
    ListingSummaryDto,
    Paginated,
    PrivacySettingsDto,
    PublicUserProfileDto,
    RegisterPushTokenRequest,
    RemovePushTokenRequest,
    ReviewSummaryDto,
    ReceivedReviewDto,
    PendingReviewOrderDto,
    SetDefaultMethodRequest,
    SetupIntentResponse,
    TransactionReminderSettingsDto,
    UserProfileUpdateRequest,
    VerificationStatusDto,
    VerificationSubmitRequest,
)
from app import stripe_service
from app.pagination import paginate
from app.routers.region_safety import REGION_DATA
from app.serializers import (
    address_to_dto,
    credit_profile,
    listing_to_summary,
    payment_to_dto,
    payout_to_dto,
    public_user_profile,
    received_review_to_dto,
    pending_review_order_to_dto,
    review_summary,
    settings_to_notification,
    settings_to_privacy,
    settings_to_transaction_reminders,
    user_to_dto,
    verification_to_dto,
)

KNOWN_CITY_NAMES = {city.name for region in REGION_DATA for city in region.cities}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
WECHAT_PAYOUT_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_-]{6,128}$")


def _payout_provider_ready(method_type: str) -> bool:
    if method_type == "bank":
        return settings.stripe_enabled
    if method_type == "paypal":
        return settings.paypal_payout_enabled
    if method_type == "alipay":
        return settings.alipay_payout_enabled
    if method_type == "wechat":
        return settings.wechat_payout_enabled
    return False


def _payout_method_label(method_type: str) -> str:
    return {
        "bank": "Australian bank account",
        "paypal": "PayPal",
        "alipay": "Alipay",
        "wechat": "WeChat Pay",
    }.get(method_type, method_type)


def _normalize_payout_account_ref(method_type: str, account_ref: str) -> str:
    value = account_ref.strip()
    if not value:
        raise HTTPException(
            status_code=422,
            detail={"code": "VALIDATION_ERROR", "message": "Payout account is required", "details": {}},
        )
    if method_type == "paypal":
        value = value.lower()
        if not EMAIL_RE.match(value):
            raise HTTPException(
                status_code=422,
                detail={"code": "VALIDATION_ERROR", "message": "Enter a valid PayPal email", "details": {}},
            )
        return value
    if method_type == "alipay":
        if len(value) < 4 or len(value) > 120:
            raise HTTPException(
                status_code=422,
                detail={"code": "VALIDATION_ERROR", "message": "Enter a valid Alipay account", "details": {}},
            )
        return value.lower() if "@" in value else value
    if method_type == "wechat":
        if not WECHAT_PAYOUT_ACCOUNT_RE.match(value):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "VALIDATION_ERROR",
                    "message": "Enter a valid WeChat payout account or OpenID",
                    "details": {},
                },
            )
        return value
    raise HTTPException(
        status_code=400,
        detail={"code": "INVALID_STATE", "message": "Unsupported payout method", "details": {}},
    )


def _valid_avatar_url(url: str) -> bool:
    trimmed = url.strip()
    if trimmed.startswith(("file://", "content://")):
        return False
    return trimmed.startswith(("http://", "https://", "/uploads/"))


class NotificationSettingsUpdate(BaseModel):
    intentAlerts: bool | None = None
    chatMessages: bool | None = None
    reviewResults: bool | None = None
    marketing: bool | None = None


class PrivacySettingsUpdate(BaseModel):
    findByPhone: bool | None = None
    showWechatBadge: bool | None = None
    personalization: bool | None = None


class TransactionReminderSettingsUpdate(BaseModel):
    payAlerts: bool | None = None
    shipAlerts: bool | None = None
    receiveAlerts: bool | None = None
    disputeAlerts: bool | None = None

router = APIRouter(tags=["users"])
payments_router = APIRouter(prefix="/payments", tags=["payments"])
payouts_router = APIRouter(prefix="/payouts", tags=["payouts"])
settings_router = APIRouter(prefix="/settings", tags=["settings"])


def _verification_submission_status(db: Session, user_id: str) -> str:
    sub = (
        db.query(VerificationSubmission)
        .filter(VerificationSubmission.user_id == user_id)
        .order_by(VerificationSubmission.created_at.desc())
        .first()
    )
    if not sub:
        return "not_submitted"
    if sub.status in ("pending", "approved", "rejected"):
        return sub.status
    return "pending"


@router.get("/users/me/profile", response_model=AuthUserDto)
def get_profile(user: User = Depends(get_current_user)):
    return user_to_dto(user)


@router.post("/users/me/push-tokens", status_code=204)
def register_push_token(
    body: RegisterPushTokenRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    token = body.token.strip()
    if not token:
        raise HTTPException(
            status_code=422,
            detail={"code": "VALIDATION_ERROR", "message": "Push token is required", "details": {}},
        )
    existing = db.query(DevicePushToken).filter(DevicePushToken.token == token).first()
    if existing:
        existing.user_id = user.id
        existing.platform = body.platform
    else:
        db.add(DevicePushToken(user_id=user.id, token=token, platform=body.platform))
    db.commit()


@router.delete("/users/me/push-tokens", status_code=204)
def remove_push_token(
    body: RemovePushTokenRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    token = body.token.strip()
    if not token:
        return
    db.query(DevicePushToken).filter(
        DevicePushToken.user_id == user.id,
        DevicePushToken.token == token,
    ).delete(synchronize_session=False)
    db.commit()


@router.patch("/users/me/profile", response_model=AuthUserDto)
def update_profile(body: UserProfileUpdateRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if body.nickname is not None:
        user.nickname = body.nickname.strip()
    if body.bio is not None:
        user.bio = body.bio
    if body.city is not None:
        city = body.city.strip()
        if city not in KNOWN_CITY_NAMES:
            raise HTTPException(status_code=400, detail="Invalid city")
        user.city = city
    if body.language is not None:
        user.language = body.language
    if body.avatarUrl is not None:
        avatar = body.avatarUrl.strip()
        if not _valid_avatar_url(avatar):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "VALIDATION_ERROR",
                    "message": "Avatar must be an uploaded http(s) or /uploads/ URL",
                    "details": {},
                },
            )
        user.avatar_url = avatar
    db.commit()
    db.refresh(user)
    return user_to_dto(user)


@router.get("/users/me/addresses", response_model=list[AddressDto])
def get_addresses(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    addrs = db.query(Address).filter(Address.user_id == user.id).all()
    return [address_to_dto(a) for a in addrs]


@router.post("/users/me/addresses", response_model=AddressDto, status_code=201)
def add_address(body: AddressCreateRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    count = db.query(Address).filter(Address.user_id == user.id).count()
    addr = Address(
        user_id=user.id,
        label=body.label,
        area=body.area,
        meetup_spot=body.meetupSpot,
        is_default=body.isDefault if body.isDefault is not None else count == 0,
    )
    if addr.is_default:
        db.query(Address).filter(Address.user_id == user.id).update({"is_default": False})
    db.add(addr)
    db.commit()
    db.refresh(addr)
    return address_to_dto(addr)


@router.patch("/users/me/addresses/{address_id}", response_model=AddressDto)
def update_address(
    address_id: str,
    body: AddressUpdateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    addr = db.query(Address).filter(Address.id == address_id, Address.user_id == user.id).first()
    if not addr:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Address not found", "details": {}})
    if body.label is not None:
        addr.label = body.label
    if body.area is not None:
        addr.area = body.area
    if body.meetupSpot is not None:
        addr.meetup_spot = body.meetupSpot
    if body.isDefault:
        db.query(Address).filter(Address.user_id == user.id).update({"is_default": False})
        addr.is_default = True
    db.commit()
    db.refresh(addr)
    return address_to_dto(addr)


@router.delete("/users/me/addresses/{address_id}", status_code=204)
def delete_address(address_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    addr = db.query(Address).filter(Address.id == address_id, Address.user_id == user.id).first()
    if addr:
        db.delete(addr)
        db.commit()
    return Response(status_code=204)


def _received_review_stats(db: Session, user_id: str, *, as_seller: bool) -> tuple[float, int]:
    q = db.query(func.avg(Review.rating), func.count(Review.id)).join(Order, Review.order_id == Order.id)
    if as_seller:
        q = q.filter(Order.seller_id == user_id, Review.reviewer_id == Order.buyer_id)
    else:
        q = q.filter(Order.buyer_id == user_id, Review.reviewer_id == Order.seller_id)
    row = q.one()
    count = int(row[1] or 0)
    if count == 0:
        return 0.0, 0
    return float(row[0] or 0.0), count


def _received_avg_rating(db: Session, user_id: str) -> float:
    avg, _ = _received_review_stats(db, user_id, as_seller=True)
    return avg


def _user_reviewed_order(db: Session, order_id: int, user_id: str) -> bool:
    return (
        db.query(Review.id)
        .filter(Review.order_id == order_id, Review.reviewer_id == user_id)
        .first()
        is not None
    )


def _pending_review_orders(db: Session, user_id: str, lang: str) -> list[PendingReviewOrderDto]:
    reviewable_statuses = ("pendingReview", "completed", "refunded")
    pending: list[PendingReviewOrderDto] = []

    buyer_orders = (
        db.query(Order)
        .options(joinedload(Order.listing), joinedload(Order.seller))
        .filter(Order.buyer_id == user_id, Order.status.in_(reviewable_statuses))
        .order_by(Order.updated_at.desc())
        .all()
    )
    for order in buyer_orders:
        if not _user_reviewed_order(db, order.id, user_id):
            pending.append(
                pending_review_order_to_dto(
                    order,
                    lang,
                    review_role="buyer",
                    counterpart_nickname=order.seller.nickname if order.seller else "",
                )
            )

    seller_orders = (
        db.query(Order)
        .options(joinedload(Order.listing), joinedload(Order.buyer))
        .filter(Order.seller_id == user_id, Order.status.in_(reviewable_statuses))
        .order_by(Order.updated_at.desc())
        .all()
    )
    for order in seller_orders:
        if not _user_reviewed_order(db, order.id, user_id):
            pending.append(
                pending_review_order_to_dto(
                    order,
                    lang,
                    review_role="seller",
                    counterpart_nickname=order.buyer.nickname if order.buyer else "",
                )
            )

    pending.sort(key=lambda row: row.orderId, reverse=True)
    return pending


def _completion_rate(db: Session, user_id: str) -> float:
    terminal = ("completed", "pendingReview", "cancelled", "refunded")
    base = db.query(Order).filter(
        or_(Order.buyer_id == user_id, Order.seller_id == user_id),
        Order.status.in_(terminal),
    )
    total = base.count()
    if total == 0:
        return 100.0
    successful = base.filter(Order.status.in_(("completed", "pendingReview"))).count()
    return round(100.0 * successful / total, 1)


@router.get("/users/me/credit", response_model=CreditProfileDto)
def get_credit(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    completed = db.query(Order).filter(Order.buyer_id == user.id, Order.status == "completed").count()
    return credit_profile(
        user.id,
        completed,
        _received_avg_rating(db, user.id),
        _completion_rate(db, user.id),
    )


@router.get("/users/me/reviews/summary", response_model=ReviewSummaryDto)
def get_review_summary(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lang = get_accept_language(request)
    pending_items = _pending_review_orders(db, user.id, lang)
    seller_avg, seller_received = _received_review_stats(db, user.id, as_seller=True)
    buyer_avg, buyer_received = _received_review_stats(db, user.id, as_seller=False)
    return review_summary(
        seller_avg,
        len(pending_items),
        seller_received,
        buyer_rating=buyer_avg,
        buyer_received_count=buyer_received,
    )


@router.get("/users/me/reviews/pending", response_model=list[PendingReviewOrderDto])
def list_pending_reviews(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lang = get_accept_language(request)
    return _pending_review_orders(db, user.id, lang)


@router.get("/users/me/reviews/received", response_model=Paginated[ReceivedReviewDto])
def list_received_reviews(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    role: str = Query("seller", pattern="^(seller|buyer)$"),
):
    q = (
        db.query(Review, Order, Listing, User)
        .join(Order, Review.order_id == Order.id)
        .join(Listing, Order.listing_id == Listing.id)
        .join(User, Review.reviewer_id == User.id)
    )
    if role == "seller":
        q = q.filter(Order.seller_id == user.id, Review.reviewer_id == Order.buyer_id)
    else:
        q = q.filter(Order.buyer_id == user.id, Review.reviewer_id == Order.seller_id)
    q = q.order_by(Review.created_at.desc())
    total = q.count()
    rows = q.offset((page - 1) * pageSize).limit(pageSize).all()
    items = [received_review_to_dto(review, order, listing, reviewer) for review, order, listing, reviewer in rows]
    return paginate(items, page, pageSize, total)


@router.get("/users/me/verification", response_model=VerificationStatusDto)
def get_verification(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return verification_to_dto(user, submission_status=_verification_submission_status(db, user.id))


@router.post("/users/me/verification/submit", response_model=VerificationStatusDto)
def submit_verification(
    body: VerificationSubmitRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pending = (
        db.query(VerificationSubmission)
        .filter(VerificationSubmission.user_id == user.id, VerificationSubmission.status == "pending")
        .first()
    )
    if pending:
        raise HTTPException(
            status_code=409,
            detail={"code": "ALREADY_PENDING", "message": "Verification already pending review", "details": {}},
        )
    submission = VerificationSubmission(
        user_id=user.id,
        legal_name=body.legalName.strip(),
        id_country=body.idCountry.upper(),
        id_front_url=body.idFrontUrl.strip(),
        id_back_url=body.idBackUrl.strip() if body.idBackUrl else None,
        business_name=body.businessName.strip() if body.businessName else None,
        business_reg_url=body.businessRegUrl.strip() if body.businessRegUrl else None,
        abn=body.abn.strip() if body.abn else None,
        status="pending",
    )
    db.add(submission)
    db.flush()
    notify_admin(
        db,
        event_type="verification_submitted",
        title="New verification submission",
        body=f"{user.nickname} submitted identity documents for review.",
        target_type="verification",
        target_id=submission.id,
        action_path=f"/verifications/{submission.id}",
    )
    db.commit()
    db.refresh(user)
    return verification_to_dto(user, submission_status="pending")


@router.post("/users/me/verification/bind", response_model=VerificationStatusDto)
def bind_verification(
    body: BindVerificationRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.type in ("identity", "business"):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "USE_SUBMIT_ENDPOINT",
                "message": "Submit identity documents via POST /users/me/verification/submit",
                "details": {},
            },
        )
    if body.type == "wechat":
        user.wechat_bound = True
    elif body.type == "alipay":
        user.alipay_bound = True
    else:
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": "Unsupported verification type", "details": {}},
        )
    db.commit()
    db.refresh(user)
    return verification_to_dto(user, submission_status=_verification_submission_status(db, user.id))


@router.get("/users/{user_id}/profile", response_model=PublicUserProfileDto)
def get_public_profile(user_id: str, request: Request, db: Session = Depends(get_db)):
    lang = get_accept_language(request)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "User not found", "details": {}},
        )
    review_row = (
        db.query(func.avg(Review.rating), func.count(Review.id))
        .join(Order, Review.order_id == Order.id)
        .filter(Order.seller_id == user.id, Review.reviewer_id == Order.buyer_id)
        .one()
    )
    avg_rating = float(review_row[0] or 0.0) if int(review_row[1] or 0) > 0 else 0.0
    review_count = int(review_row[1] or 0)
    listing_count = apply_public_listing_visibility_filter(
        db.query(Listing).filter(Listing.seller_id == user.id)
    ).count()
    follower_count = db.query(Follow).filter(Follow.followed_id == user.id).count()
    settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
    return public_user_profile(
        user,
        rating=avg_rating,
        review_count=review_count,
        listing_count=listing_count,
        follower_count=follower_count,
        settings=settings,
        lang=lang,
    )


@router.get("/users/{user_id}/listings", response_model=Paginated[ListingSummaryDto])
def get_public_listings(
    user_id: str,
    request: Request,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    lang = get_accept_language(request)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "User not found", "details": {}},
        )
    q = (
        db.query(Listing)
        .options(joinedload(Listing.seller))
        .filter(Listing.seller_id == user.id)
        .order_by(Listing.created_at.desc())
    )
    q = apply_public_listing_visibility_filter(q)
    total = q.count()
    items = q.offset((page - 1) * pageSize).limit(pageSize).all()
    return paginate([listing_to_summary(i, lang) for i in items], page, pageSize, total)


@payments_router.get("/methods", response_model=list[PaymentMethodDto])
def list_payment_methods(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not settings.stripe_enabled:
        return []
    methods = (
        db.query(PaymentMethod)
        .filter(
            PaymentMethod.user_id == user.id,
            PaymentMethod.type == "card",
            PaymentMethod.stripe_payment_method_id.isnot(None),
        )
        .order_by(PaymentMethod.is_default.desc(), PaymentMethod.id.asc())
        .all()
    )
    return [payment_to_dto(m) for m in methods]


_WALLET_LABELS = {
    "apple_pay": "Apple Pay",
    "google_pay": "Google Pay",
    "alipay": "Alipay",
    "wechat_pay": "WeChat Pay",
    "paypal": "PayPal",
}


@payments_router.post("/setup-intent", response_model=SetupIntentResponse)
def create_payment_setup_intent(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Bootstrap the mobile PaymentSheet to save a card for reuse."""
    if not settings.stripe_enabled:
        return SetupIntentResponse(
            publishableKey="",
            customerId=f"cus_sim_{user.id[:8]}",
            ephemeralKey="",
            setupIntentClientSecret="",
            simulated=True,
        )
    customer_id = stripe_service.ensure_customer(user)
    if user.stripe_customer_id != customer_id:
        user.stripe_customer_id = customer_id
        db.commit()
    data = stripe_service.create_setup_intent(customer_id)
    return SetupIntentResponse(**data, simulated=False)


@payments_router.post("/methods", response_model=PaymentMethodDto, status_code=201)
def add_payment_method(body: AddPaymentMethodRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Persist a connected card after Stripe has verified it through SetupIntent."""
    if body.type != "card":
        raise HTTPException(
            status_code=400,
            detail={
                "code": "PAYMENT_METHOD_UNSUPPORTED",
                "message": "Only reusable card methods can be saved in settings",
                "details": {},
            },
        )
    if not settings.stripe_enabled:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "PAYMENT_SETUP_UNAVAILABLE",
                "message": "Secure card saving is not configured",
                "details": {},
            },
        )
    if not body.stripePaymentMethodId:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "PAYMENT_METHOD_REQUIRED",
                "message": "A verified Stripe payment method is required",
                "details": {},
            },
        )
    brand = exp_month = exp_year = stripe_pm_id = last4 = None

    try:
        pm_obj = stripe_service.retrieve_payment_method(body.stripePaymentMethodId)
    except Exception:
        raise HTTPException(
            status_code=400,
            detail={"code": "PAYMENT_METHOD_INVALID", "message": "Could not verify the payment method with Stripe", "details": {}},
        )
    stripe_pm_id = pm_obj.get("id")
    card = pm_obj.get("card") or {}
    brand, last4 = card.get("brand"), card.get("last4")
    exp_month, exp_year = card.get("exp_month"), card.get("exp_year")
    if not user.stripe_customer_id:
        user.stripe_customer_id = stripe_service.ensure_customer(user)

    if body.type == "card":
        label = f"{brand.title()} •••• {last4}" if brand and last4 else (f"Card •••• {last4}" if last4 else "Card")
    else:
        label = _WALLET_LABELS.get(body.type, body.type)

    count = (
        db.query(PaymentMethod)
        .filter(
            PaymentMethod.user_id == user.id,
            PaymentMethod.type == "card",
            PaymentMethod.stripe_payment_method_id.isnot(None),
        )
        .count()
    )
    pm = PaymentMethod(
        user_id=user.id,
        type=body.type,
        label=label,
        last4=last4 if body.type == "card" else None,
        brand=brand,
        exp_month=exp_month,
        exp_year=exp_year,
        stripe_payment_method_id=stripe_pm_id,
        is_default=count == 0,
    )
    db.add(pm)
    db.commit()
    db.refresh(pm)
    return payment_to_dto(pm)


@payments_router.post("/methods/sync", response_model=list[PaymentMethodDto])
def sync_payment_methods(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Reconcile saved cards with Stripe after a PaymentSheet SetupIntent completes
    (PaymentSheet doesn't return the pm id to the app). No-op without Stripe."""
    if not settings.stripe_enabled or not user.stripe_customer_id:
        return []
    try:
        stripe_pms = stripe_service.list_card_payment_methods(user.stripe_customer_id)
    except Exception:
        stripe_pms = []
    existing = {
        m.stripe_payment_method_id: m
        for m in db.query(PaymentMethod).filter(
            PaymentMethod.user_id == user.id, PaymentMethod.stripe_payment_method_id.isnot(None)
        ).all()
    }
    has_any = (
        db.query(PaymentMethod)
        .filter(
            PaymentMethod.user_id == user.id,
            PaymentMethod.type == "card",
            PaymentMethod.stripe_payment_method_id.isnot(None),
        )
        .count()
        > 0
    )
    for spm in stripe_pms:
        pmid = spm.get("id")
        card = spm.get("card") or {}
        brand, last4 = card.get("brand"), card.get("last4")
        if pmid in existing:
            row = existing[pmid]
            row.brand, row.last4 = brand, last4
            row.exp_month, row.exp_year = card.get("exp_month"), card.get("exp_year")
        else:
            db.add(
                PaymentMethod(
                    user_id=user.id,
                    type="card",
                    label=f"{brand.title()} •••• {last4}" if brand and last4 else "Card",
                    last4=last4,
                    brand=brand,
                    exp_month=card.get("exp_month"),
                    exp_year=card.get("exp_year"),
                    stripe_payment_method_id=pmid,
                    is_default=not has_any,
                )
            )
            has_any = True
    db.commit()
    return [
        payment_to_dto(m)
        for m in db.query(PaymentMethod)
        .filter(
            PaymentMethod.user_id == user.id,
            PaymentMethod.type == "card",
            PaymentMethod.stripe_payment_method_id.isnot(None),
        )
        .order_by(PaymentMethod.is_default.desc(), PaymentMethod.id.asc())
        .all()
    ]


@payments_router.delete("/methods/{method_id}", status_code=204)
def remove_payment_method(
    method_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pm = (
        db.query(PaymentMethod)
        .filter(
            PaymentMethod.id == method_id,
            PaymentMethod.user_id == user.id,
            PaymentMethod.type == "card",
            PaymentMethod.stripe_payment_method_id.isnot(None),
        )
        .first()
    )
    if not pm:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Payment method not found", "details": {}})
    if settings.stripe_enabled and getattr(pm, "stripe_payment_method_id", None):
        stripe_service.detach_payment_method(pm.stripe_payment_method_id)
    was_default = pm.is_default
    db.delete(pm)
    db.commit()
    if was_default:
        remaining = (
            db.query(PaymentMethod)
            .filter(
                PaymentMethod.user_id == user.id,
                PaymentMethod.type == "card",
                PaymentMethod.stripe_payment_method_id.isnot(None),
            )
            .order_by(PaymentMethod.id.asc())
            .first()
        )
        if remaining:
            remaining.is_default = True
            db.commit()
    return Response(status_code=204)


@payments_router.patch("/methods/{method_id}", response_model=PaymentMethodDto)
def set_default_payment_method(
    method_id: str,
    body: SetDefaultMethodRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pm = (
        db.query(PaymentMethod)
        .filter(
            PaymentMethod.id == method_id,
            PaymentMethod.user_id == user.id,
            PaymentMethod.type == "card",
            PaymentMethod.stripe_payment_method_id.isnot(None),
        )
        .first()
    )
    if not pm:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Payment method not found", "details": {}})
    if body.isDefault:
        for method in (
            db.query(PaymentMethod)
            .filter(
                PaymentMethod.user_id == user.id,
                PaymentMethod.type == "card",
                PaymentMethod.stripe_payment_method_id.isnot(None),
            )
            .all()
        ):
            method.is_default = method.id == method_id
    else:
        pm.is_default = False
    db.commit()
    db.refresh(pm)
    return payment_to_dto(pm)


@payouts_router.get("/methods", response_model=list[PayoutMethodDto])
def list_payout_methods(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    methods = (
        db.query(PayoutMethod)
        .filter(PayoutMethod.user_id == user.id)
        .order_by(PayoutMethod.is_default.desc(), PayoutMethod.type.asc(), PayoutMethod.id.asc())
        .all()
    )
    return [payout_to_dto(m) for m in methods]


@payouts_router.post("/methods", response_model=PayoutMethodDto, status_code=201)
def add_payout_method(body: AddPayoutMethodRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if body.type == "bank":
        raise HTTPException(
            status_code=400,
            detail={
                "code": "USE_CONNECT_ONBOARDING",
                "message": "Bank payouts must be connected through Stripe onboarding",
                "details": {},
            },
        )
    if body.type == "paypal":
        raise HTTPException(
            status_code=400,
            detail={
                "code": "USE_PAYPAL_ONBOARDING",
                "message": "PayPal payouts must be connected through PayPal seller onboarding",
                "details": {},
            },
        )
    if body.type == "alipay" and not user.alipay_bound:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PAYOUT_BIND_REQUIRED",
                "message": "Link Alipay in Account Safety before adding an Alipay payout destination",
                "details": {"type": "alipay", "screen": "accountSafety"},
            },
        )
    if body.type == "wechat" and not user.wechat_bound:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PAYOUT_BIND_REQUIRED",
                "message": "Link WeChat in Account Safety before adding a WeChat payout destination",
                "details": {"type": "wechat", "screen": "accountSafety"},
            },
        )
    if not _payout_provider_ready(body.type):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "PAYOUT_PROVIDER_NOT_READY",
                "message": f"{_payout_method_label(body.type)} payouts are not configured on the platform",
                "details": {},
            },
        )
    account_ref = _normalize_payout_account_ref(body.type, body.accountRef or body.accountToken or "")
    count = db.query(PayoutMethod).filter(PayoutMethod.user_id == user.id).count()
    pm = (
        db.query(PayoutMethod)
        .filter(PayoutMethod.user_id == user.id, PayoutMethod.type == body.type)
        .first()
    )
    if not pm:
        pm = PayoutMethod(
            user_id=user.id,
            type=body.type,
            label=_payout_method_label(body.type),
            is_default=count == 0,
        )
        db.add(pm)
    pm.label = _payout_method_label(body.type)
    pm.account_ref = account_ref
    pm.last4 = None
    pm.payouts_enabled = True
    db.commit()
    db.refresh(pm)
    return payout_to_dto(pm)

    last4 = body.accountToken[-4:] if len(body.accountToken) >= 4 else "0000"
    labels = {
        "bank": f"Bank •••• {last4}",
        "paypal": "PayPal",
        "alipay": "Alipay",
        "wechat": "WeChat",
    }
    count = db.query(PayoutMethod).filter(PayoutMethod.user_id == user.id).count()
    pm = PayoutMethod(
        user_id=user.id,
        type=body.type,
        label=labels.get(body.type, body.type),
        last4=last4 if body.type == "bank" else None,
        is_default=count == 0,
    )
    db.add(pm)
    db.commit()
    db.refresh(pm)
    return payout_to_dto(pm)


@payouts_router.delete("/methods/{method_id}", status_code=204)
def remove_payout_method(
    method_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pm = db.query(PayoutMethod).filter(PayoutMethod.id == method_id, PayoutMethod.user_id == user.id).first()
    if not pm:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Payout method not found", "details": {}})
    was_default = pm.is_default
    db.delete(pm)
    db.commit()
    if was_default:
        remaining = (
            db.query(PayoutMethod)
            .filter(PayoutMethod.user_id == user.id)
            .order_by(PayoutMethod.id.asc())
            .first()
        )
        if remaining:
            remaining.is_default = True
            db.commit()
    return Response(status_code=204)


@payouts_router.patch("/methods/{method_id}", response_model=PayoutMethodDto)
def set_default_payout_method(
    method_id: str,
    body: SetDefaultMethodRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pm = db.query(PayoutMethod).filter(PayoutMethod.id == method_id, PayoutMethod.user_id == user.id).first()
    if not pm:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Payout method not found", "details": {}})
    if body.isDefault:
        for method in db.query(PayoutMethod).filter(PayoutMethod.user_id == user.id).all():
            method.is_default = method.id == method_id
    else:
        pm.is_default = False
    db.commit()
    db.refresh(pm)
    return payout_to_dto(pm)


@payouts_router.post("/connect/link", response_model=ConnectOnboardingResponse)
def create_payout_onboarding_link(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Create/return a Stripe Connect Express onboarding URL for bank payouts."""
    if not settings.stripe_enabled:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "PAYOUT_PROVIDER_NOT_READY",
                "message": "Stripe bank payouts are not configured on the platform",
                "details": {},
            },
        )
    try:
        account_id = stripe_service.ensure_connect_account(user)
    except stripe_service.StripeConnectCountryMismatch as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PAYOUT_COUNTRY_MISMATCH",
                "message": str(exc),
                "details": {"requiredCountry": settings.stripe_connect_country.upper()},
            },
        ) from exc
    if user.stripe_connect_id != account_id:
        user.stripe_connect_id = account_id
        db.commit()
    url = stripe_service.create_account_onboarding_link(account_id)
    return ConnectOnboardingResponse(url=url, simulated=False)


@payouts_router.post("/paypal/link", response_model=ConnectOnboardingResponse)
def create_paypal_seller_onboarding_link(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a one-use PayPal Partner Referral link for this seller."""
    if not settings.paypal_payout_enabled or not settings.paypal_partner_attribution_id.strip():
        raise HTTPException(
            status_code=400,
            detail={
                "code": "PAYOUT_PROVIDER_NOT_READY",
                "message": "PayPal seller onboarding is not configured on the platform",
                "details": {},
            },
        )
    tracking_id = secrets.token_urlsafe(32)
    method = (
        db.query(PayoutMethod)
        .filter(PayoutMethod.user_id == user.id, PayoutMethod.type == "paypal")
        .first()
    )
    if not method:
        count = db.query(PayoutMethod).filter(PayoutMethod.user_id == user.id).count()
        method = PayoutMethod(
            user_id=user.id,
            type="paypal",
            label="PayPal",
            is_default=count == 0,
        )
        db.add(method)
    method.paypal_tracking_id = tracking_id
    method.paypal_permissions_granted = False
    method.paypal_email_confirmed = False
    method.payouts_enabled = False
    db.commit()

    return_url = (
        f"{settings.base_url.rstrip('/')}/v1/payouts/paypal/return"
        f"?trackingId={tracking_id}"
    )
    try:
        payload = paypal_partner_service.create_seller_referral(
            tracking_id=tracking_id,
            return_url=return_url,
        )
        url = paypal_partner_service.referral_action_url(payload)
    except paypal_partner_service.PayPalPartnerError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "PAYMENT_PROVIDER_ERROR", "message": str(exc), "details": {}},
        ) from exc
    return ConnectOnboardingResponse(url=url, simulated=False)


def _paypal_true(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


@payouts_router.get("/paypal/return", include_in_schema=False)
def paypal_seller_onboarding_return(
    trackingId: str,
    merchantId: str | None = None,
    merchantIdInPayPal: str | None = None,
    permissionsGranted: str | None = None,
    consentStatus: str | None = None,
    isEmailConfirmed: str | None = None,
    db: Session = Depends(get_db),
):
    """Persist PayPal's seller identity; no seller ID is ever entered manually."""
    tracking_id = merchantId or trackingId
    method = (
        db.query(PayoutMethod)
        .filter(PayoutMethod.type == "paypal", PayoutMethod.paypal_tracking_id == tracking_id)
        .first()
    )
    if not method or not merchantIdInPayPal:
        return RedirectResponse("heishi:///settings/payout?paypalConnect=error", status_code=302)

    permissions = _paypal_true(permissionsGranted)
    consent = _paypal_true(consentStatus)
    email_confirmed = _paypal_true(isEmailConfirmed)
    method.paypal_merchant_id = merchantIdInPayPal.strip()
    method.account_ref = merchantIdInPayPal.strip()
    method.paypal_permissions_granted = permissions and consent
    method.paypal_email_confirmed = email_confirmed
    method.payouts_enabled = permissions and consent and email_confirmed
    db.commit()
    result = "return" if method.payouts_enabled else "pending"
    return RedirectResponse(f"heishi:///settings/payout?paypalConnect={result}", status_code=302)


@payouts_router.get("/paypal/status", response_model=ConnectStatusResponse)
def get_paypal_seller_onboarding_status(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    method = (
        db.query(PayoutMethod)
        .filter(PayoutMethod.user_id == user.id, PayoutMethod.type == "paypal")
        .first()
    )
    if not method:
        return ConnectStatusResponse(connected=False, detailsSubmitted=False, payoutsEnabled=False)
    return ConnectStatusResponse(
        connected=bool(method.paypal_merchant_id),
        detailsSubmitted=bool(method.paypal_permissions_granted),
        payoutsEnabled=bool(method.payouts_enabled),
    )


@payouts_router.get("/connect/return", include_in_schema=False)
def payout_connect_return():
    """Hand control from Stripe's required web callback back to the payout screen."""
    return RedirectResponse("heishi:///settings/payout?stripeConnect=return", status_code=302)


@payouts_router.get("/connect/refresh", include_in_schema=False)
def payout_connect_refresh():
    """Return to the payout screen so the seller can request a fresh Account Link."""
    return RedirectResponse("heishi:///settings/payout?stripeConnect=refresh", status_code=302)


@payouts_router.get("/connect/status", response_model=ConnectStatusResponse)
def get_payout_connect_status(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Poll the connected account after onboarding; syncs a `bank` payout row once the
    seller's payouts are enabled so the payout list reflects the real bank account."""
    if not settings.stripe_enabled or not user.stripe_connect_id:
        return ConnectStatusResponse(connected=False, detailsSubmitted=False, payoutsEnabled=False)
    account = stripe_service.retrieve_account(user.stripe_connect_id)
    details = bool(account.get("details_submitted"))
    payouts_enabled = bool(account.get("payouts_enabled"))
    if details:
        external = (account.get("external_accounts") or {}).get("data") or []
        last4 = external[0].get("last4") if external else None
        bank = (
            db.query(PayoutMethod)
            .filter(PayoutMethod.user_id == user.id, PayoutMethod.type == "bank")
            .first()
        )
        if not bank:
            count = db.query(PayoutMethod).filter(PayoutMethod.user_id == user.id).count()
            bank = PayoutMethod(
                user_id=user.id,
                type="bank",
                label="Australian bank account",
                is_default=count == 0,
            )
            db.add(bank)
        bank.stripe_external_account_id = user.stripe_connect_id
        bank.payouts_enabled = payouts_enabled
        bank.account_ref = None
        if last4:
            bank.last4 = last4
            bank.label = f"Bank •••• {last4}"
        db.commit()
        if bank.label != "Australian bank account":
            bank.label = "Australian bank account"
            db.commit()
    return ConnectStatusResponse(connected=True, detailsSubmitted=details, payoutsEnabled=payouts_enabled)


@settings_router.get("/notifications", response_model=NotificationSettingsDto)
def get_notification_settings(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    s = get_or_create_settings(db, user.id)
    return settings_to_notification(s)


@settings_router.patch("/notifications", response_model=NotificationSettingsDto)
def update_notification_settings(
    body: NotificationSettingsUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    s = get_or_create_settings(db, user.id)
    data = body.model_dump(exclude_unset=True)
    mapping = {
        "intentAlerts": "intent_alerts",
        "chatMessages": "chat_messages",
        "reviewResults": "review_results",
        "marketing": "marketing",
    }
    for k, v in data.items():
        if k in mapping:
            setattr(s, mapping[k], v)
    db.commit()
    db.refresh(s)
    return settings_to_notification(s)


@settings_router.get("/privacy", response_model=PrivacySettingsDto)
def get_privacy_settings(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    s = get_or_create_settings(db, user.id)
    return settings_to_privacy(s)


@settings_router.patch("/privacy", response_model=PrivacySettingsDto)
def update_privacy_settings(
    body: PrivacySettingsUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    s = get_or_create_settings(db, user.id)
    data = body.model_dump(exclude_unset=True)
    mapping = {
        "findByPhone": "find_by_phone",
        "showWechatBadge": "show_wechat_badge",
        "personalization": "personalization",
    }
    for k, v in data.items():
        if k in mapping:
            setattr(s, mapping[k], v)
    db.commit()
    db.refresh(s)
    return settings_to_privacy(s)


@settings_router.get("/transaction-reminders", response_model=TransactionReminderSettingsDto)
def get_transaction_reminder_settings(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    s = get_or_create_settings(db, user.id)
    return settings_to_transaction_reminders(s)


@settings_router.patch("/transaction-reminders", response_model=TransactionReminderSettingsDto)
def update_transaction_reminder_settings(
    body: TransactionReminderSettingsUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    s = get_or_create_settings(db, user.id)
    data = body.model_dump(exclude_unset=True)
    mapping = {
        "payAlerts": "remind_pay",
        "shipAlerts": "remind_ship",
        "receiveAlerts": "remind_receive",
        "disputeAlerts": "remind_dispute",
    }
    for k, v in data.items():
        if k in mapping:
            setattr(s, mapping[k], v)
    db.commit()
    db.refresh(s)
    return settings_to_transaction_reminders(s)


@settings_router.post("/cache/clear", response_model=CacheClearResponse)
def clear_cache(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = (
        db.query(ViewHistory)
        .filter(ViewHistory.user_id == user.id)
        .delete(synchronize_session=False)
    )
    db.commit()
    return CacheClearResponse(freedBytes=max(rows, 0) * 256)


@settings_router.get("/data-export", response_model=DataExportDto)
def export_user_data(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    settings_row = get_or_create_settings(db, user.id)
    addresses = db.query(Address).filter(Address.user_id == user.id).order_by(Address.id.asc()).all()
    return DataExportDto(
        exportedAt=datetime.now(timezone.utc).isoformat(),
        profile=user_to_dto(user),
        notificationSettings=settings_to_notification(settings_row),
        privacySettings=settings_to_privacy(settings_row),
        transactionReminderSettings=settings_to_transaction_reminders(settings_row),
        addresses=[address_to_dto(a) for a in addresses],
        verification=verification_to_dto(user, submission_status=_verification_submission_status(db, user.id)),
    )
