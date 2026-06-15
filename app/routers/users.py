from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.catalog_helpers import get_or_create_settings
from app.database import get_db
from app.models import Address, Order, PaymentMethod, PayoutMethod, Review, User
from app.schemas import (
    AddPaymentMethodRequest,
    AddPayoutMethodRequest,
    AddressCreateRequest,
    AddressDto,
    AuthUserDto,
    CacheClearResponse,
    CreditProfileDto,
    NotificationSettingsDto,
    PaymentMethodDto,
    PayoutMethodDto,
    PrivacySettingsDto,
    ReviewSummaryDto,
    UserProfileUpdateRequest,
    VerificationStatusDto,
)
from app.serializers import (
    address_to_dto,
    credit_profile,
    payment_to_dto,
    payout_to_dto,
    review_summary,
    settings_to_notification,
    settings_to_privacy,
    user_to_dto,
    verification_to_dto,
)


class NotificationSettingsUpdate(BaseModel):
    intentAlerts: bool | None = None
    chatMessages: bool | None = None
    reviewResults: bool | None = None
    marketing: bool | None = None


class PrivacySettingsUpdate(BaseModel):
    findByPhone: bool | None = None
    showWechatBadge: bool | None = None
    personalization: bool | None = None

router = APIRouter(tags=["users"])
payments_router = APIRouter(prefix="/payments", tags=["payments"])
payouts_router = APIRouter(prefix="/payouts", tags=["payouts"])
settings_router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/users/me/profile", response_model=AuthUserDto)
def get_profile(user: User = Depends(get_current_user)):
    return user_to_dto(user)


@router.patch("/users/me/profile", response_model=AuthUserDto)
def update_profile(body: UserProfileUpdateRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if body.nickname is not None:
        user.nickname = body.nickname.strip()
    if body.bio is not None:
        user.bio = body.bio
    if body.city is not None:
        user.city = body.city
    if body.language is not None:
        user.language = body.language
    if body.avatarUrl is not None:
        user.avatar_url = body.avatarUrl
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
    body: AddressCreateRequest,
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


@router.get("/users/me/credit", response_model=CreditProfileDto)
def get_credit(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    completed = db.query(Order).filter(Order.buyer_id == user.id, Order.status == "completed").count()
    avg_rating = db.query(func.avg(Review.rating)).filter(Review.reviewer_id == user.id).scalar() or 5.0
    return credit_profile(user.id, completed, float(avg_rating))


@router.get("/users/me/reviews/summary", response_model=ReviewSummaryDto)
def get_review_summary(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    pending = db.query(Order).filter(Order.buyer_id == user.id, Order.status == "pendingReview").count()
    avg_rating = db.query(func.avg(Review.rating)).filter(Review.reviewer_id == user.id).scalar() or 5.0
    return review_summary(float(avg_rating), pending)


@router.get("/users/me/verification", response_model=VerificationStatusDto)
def get_verification(user: User = Depends(get_current_user)):
    return verification_to_dto(user)


@payments_router.get("/methods", response_model=list[PaymentMethodDto])
def list_payment_methods(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    methods = db.query(PaymentMethod).filter(PaymentMethod.user_id == user.id).all()
    return [payment_to_dto(m) for m in methods]


@payments_router.post("/methods", response_model=PaymentMethodDto, status_code=201)
def add_payment_method(body: AddPaymentMethodRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    last4 = body.token[-4:] if len(body.token) >= 4 else "0000"
    labels = {"card": f"Card •••• {last4}", "apple_pay": "Apple Pay", "paypal": "PayPal"}
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


@payouts_router.get("/methods", response_model=list[PayoutMethodDto])
def list_payout_methods(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    methods = db.query(PayoutMethod).filter(PayoutMethod.user_id == user.id).all()
    return [payout_to_dto(m) for m in methods]


@payouts_router.post("/methods", response_model=PayoutMethodDto, status_code=201)
def add_payout_method(body: AddPayoutMethodRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    last4 = body.accountToken[-4:] if len(body.accountToken) >= 4 else "0000"
    labels = {"bank": f"Bank •••• {last4}", "paypal": "PayPal"}
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


@settings_router.post("/cache/clear", response_model=CacheClearResponse)
def clear_cache(user: User = Depends(get_current_user)):
    return CacheClearResponse(freedBytes=0)
