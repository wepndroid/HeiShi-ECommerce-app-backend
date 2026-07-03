from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.auth import get_accept_language, get_current_user
from app.catalog_helpers import get_or_create_settings
from app.database import get_db
from app.models import Address, DevicePushToken, Follow, Listing, Order, PaymentMethod, PayoutMethod, Review, User, UserSettings, VerificationSubmission, ViewHistory
from app.schemas import (
    AddPaymentMethodRequest,
    AddPayoutMethodRequest,
    AddressCreateRequest,
    AddressUpdateRequest,
    AddressDto,
    AuthUserDto,
    BindVerificationRequest,
    CacheClearResponse,
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
    TransactionReminderSettingsDto,
    UserProfileUpdateRequest,
    VerificationStatusDto,
    VerificationSubmitRequest,
)
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
    reviewable_statuses = ("pendingReview", "completed")
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
    terminal = ("completed", "pendingReview", "cancelled")
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
    db.add(
        VerificationSubmission(
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
    listing_count = (
        db.query(Listing).filter(Listing.seller_id == user.id, Listing.status == "active").count()
    )
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
        .filter(Listing.seller_id == user.id, Listing.status == "active")
        .order_by(Listing.created_at.desc())
    )
    total = q.count()
    items = q.offset((page - 1) * pageSize).limit(pageSize).all()
    return paginate([listing_to_summary(i, lang) for i in items], page, pageSize, total)


@payments_router.get("/methods", response_model=list[PaymentMethodDto])
def list_payment_methods(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    methods = db.query(PaymentMethod).filter(PaymentMethod.user_id == user.id).all()
    return [payment_to_dto(m) for m in methods]


@payments_router.post("/methods", response_model=PaymentMethodDto, status_code=201)
def add_payment_method(body: AddPaymentMethodRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    last4 = body.token[-4:] if len(body.token) >= 4 else "0000"
    labels = {
        "card": f"Card •••• {last4}",
        "apple_pay": "Apple Pay",
        "google_pay": "Google Pay",
        "alipay": "Alipay",
        "wechat_pay": "WeChat Pay",
        "paypal": "PayPal",
    }
    count = db.query(PaymentMethod).filter(PaymentMethod.user_id == user.id).count()
    pm = PaymentMethod(
        user_id=user.id,
        type=body.type,
        label=labels.get(body.type, body.type),
        last4=last4 if body.type == "card" else None,
        is_default=count == 0,
    )
    db.add(pm)
    db.commit()
    db.refresh(pm)
    return payment_to_dto(pm)


@payments_router.delete("/methods/{method_id}", status_code=204)
def remove_payment_method(
    method_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pm = db.query(PaymentMethod).filter(PaymentMethod.id == method_id, PaymentMethod.user_id == user.id).first()
    if not pm:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Payment method not found", "details": {}})
    db.delete(pm)
    db.commit()
    return Response(status_code=204)


@payments_router.patch("/methods/{method_id}", response_model=PaymentMethodDto)
def set_default_payment_method(
    method_id: str,
    body: SetDefaultMethodRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pm = db.query(PaymentMethod).filter(PaymentMethod.id == method_id, PaymentMethod.user_id == user.id).first()
    if not pm:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Payment method not found", "details": {}})
    if body.isDefault:
        for method in db.query(PaymentMethod).filter(PaymentMethod.user_id == user.id).all():
            method.is_default = method.id == method_id
    else:
        pm.is_default = False
    db.commit()
    db.refresh(pm)
    return payment_to_dto(pm)


@payouts_router.get("/methods", response_model=list[PayoutMethodDto])
def list_payout_methods(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    methods = db.query(PayoutMethod).filter(PayoutMethod.user_id == user.id).all()
    return [payout_to_dto(m) for m in methods]


@payouts_router.post("/methods", response_model=PayoutMethodDto, status_code=201)
def add_payout_method(body: AddPayoutMethodRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
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
