from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    """Normalize DB datetimes to UTC-aware (SQLite returns naive values)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    nickname: Mapped[str] = mapped_column(String(50))
    # Nullable: OAuth (Google/Apple/WeChat) users have no phone at sign-up. UNIQUE still
    # holds because SQLite/Postgres allow multiple NULLs in a unique column.
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, index=True, nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    language: Mapped[str] = mapped_column(String(5), default="en")
    heishi_id: Mapped[str] = mapped_column(String(20), unique=True)
    phone_verified: Mapped[bool] = mapped_column(Boolean, default=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    wechat_bound: Mapped[bool] = mapped_column(Boolean, default=False)
    wechat_openid: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    wechat_unionid: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    alipay_bound: Mapped[bool] = mapped_column(Boolean, default=False)
    identity_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    business_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    account_status: Mapped[str] = mapped_column(String(20), default="normal", index=True)
    admin_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    banned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ban_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Moderation controls (admin MVP): mute silences chat, publish-restrict blocks new
    # listings, flag marks the account abnormal for risk review. Each keeps its own reason.
    is_muted: Mapped[bool] = mapped_column(Boolean, default=False)
    muted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mute_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    publish_restricted: Mapped[bool] = mapped_column(Boolean, default=False)
    publish_restricted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publish_restrict_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    flag_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    stripe_connect_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Buyer-side Stripe Customer (holds saved cards/wallets for PaymentSheet reuse).
    stripe_customer_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    preferred_display_currency: Mapped[str] = mapped_column(String(3), default="aud")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    listings: Mapped[list[Listing]] = relationship(back_populates="seller")
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(back_populates="user")


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(255), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="refresh_tokens")


class PhoneOtp(Base):
    __tablename__ = "phone_otps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    phone: Mapped[str] = mapped_column(String(20), index=True)
    purpose: Mapped[str] = mapped_column(String(20), default="register")
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("phone", "purpose", name="uq_phone_otp_purpose"),)


class Listing(Base):
    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    seller_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(20), default="product")
    title: Mapped[str] = mapped_column(String(200))
    title_zh: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    description_zh: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[float] = mapped_column(Float)
    category_key: Mapped[str] = mapped_column(String(50))
    tag_key: Mapped[str] = mapped_column(String(50), default="")
    condition_key: Mapped[str | None] = mapped_column(String(50), nullable=True)
    location_label: Mapped[str] = mapped_column(String(100))
    region_state: Mapped[str] = mapped_column(String(10), default="VIC")
    region_city: Mapped[str] = mapped_column(String(50), default="Melbourne")
    region_area: Mapped[str] = mapped_column(String(50), default="Clayton")
    image_url: Mapped[str] = mapped_column(String(500))
    images_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    negotiable: Mapped[bool] = mapped_column(Boolean, default=False)
    escrow_supported: Mapped[bool] = mapped_column(Boolean, default=True)
    meet_in_public: Mapped[bool] = mapped_column(Boolean, default=True)
    pickup_methods_json: Mapped[str] = mapped_column(Text, default='["meetup"]')
    bundle_meta_json: Mapped[str] = mapped_column(Text, default="{}")
    service_icon: Mapped[str | None] = mapped_column(String(30), nullable=True)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    favorite_count: Mapped[int] = mapped_column(Integer, default=0)
    review_status: Mapped[str] = mapped_column(String(20), default="pendingReview", index=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    is_recommended: Mapped[bool] = mapped_column(Boolean, default=False)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    promotion_click_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    seller: Mapped[User] = relationship(back_populates="listings")

    @property
    def images(self) -> list[str]:
        try:
            return json.loads(self.images_json)
        except json.JSONDecodeError:
            return [self.image_url]

    @images.setter
    def images(self, value: list[str]) -> None:
        self.images_json = json.dumps(value)
        if value:
            self.image_url = value[0]

    @property
    def pickup_methods(self) -> list[str]:
        try:
            return json.loads(self.pickup_methods_json)
        except json.JSONDecodeError:
            return ["meetup"]

    @pickup_methods.setter
    def pickup_methods(self, value: list[str]) -> None:
        self.pickup_methods_json = json.dumps(value)

    @property
    def bundle_meta(self) -> dict:
        try:
            parsed = json.loads(self.bundle_meta_json)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}

    @bundle_meta.setter
    def bundle_meta(self, value: dict) -> None:
        self.bundle_meta_json = json.dumps(value)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    buyer_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), index=True)
    seller_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), default="pendingPay", index=True)
    amount: Mapped[float] = mapped_column(Float)
    escrow_fee: Mapped[float] = mapped_column(Float, default=0.0)
    delivery_method: Mapped[str] = mapped_column(String(50), default="meetup")
    payment_method_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    bundle_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    coupon_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    discount_amount: Mapped[float] = mapped_column(Float, default=0.0)
    payment_method: Mapped[str | None] = mapped_column(String(30), nullable=True)
    psp: Mapped[str | None] = mapped_column(String(20), nullable=True)
    payment_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    psp_payment_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    psp_transaction_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    charge_currency: Mapped[str] = mapped_column(String(3), default="aud")
    amount_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    display_amount_cny: Mapped[float | None] = mapped_column(Float, nullable=True)
    payout_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    payout_status: Mapped[str] = mapped_column(String(30), default="pending")
    payout_provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    payout_method_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payout_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payout_failure_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    payout_failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    payout_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payout_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payout_reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payout_reversal_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_abnormal: Mapped[bool] = mapped_column(Boolean, default=False)
    admin_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    dispute_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    dispute_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    dispute_evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    auto_confirm_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    listing: Mapped[Listing] = relationship()
    buyer: Mapped[User] = relationship(foreign_keys=[buyer_id])
    seller: Mapped[User] = relationship(foreign_keys=[seller_id])


class Favorite(Base):
    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("user_id", "listing_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ViewHistory(Base):
    __tablename__ = "view_history"
    __table_args__ = (UniqueConstraint("user_id", "listing_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), index=True)
    viewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Follow(Base):
    __tablename__ = "follows"
    __table_args__ = (UniqueConstraint("follower_id", "followed_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    follower_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    followed_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Coupon(Base):
    __tablename__ = "coupons"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[float] = mapped_column(Float)
    description: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="available")


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), index=True)
    buyer_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    seller_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    last_message_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    buyer_unread: Mapped[int] = mapped_column(Integer, default=0)
    seller_unread: Mapped[int] = mapped_column(Integer, default=0)
    buyer_read_inbox_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    seller_read_inbox_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    buyer_marked_unread: Mapped[bool] = mapped_column(Boolean, default=False)
    seller_marked_unread: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    listing: Mapped[Listing] = relationship()
    buyer: Mapped[User] = relationship(foreign_keys=[buyer_id])
    seller: Mapped[User] = relationship(foreign_keys=[seller_id])
    messages: Mapped[list[Message]] = relationship(back_populates="conversation")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    sender_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Address(Base):
    __tablename__ = "addresses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    label: Mapped[str] = mapped_column(String(100))
    area: Mapped[str] = mapped_column(String(100))
    meetup_spot: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)


class PaymentMethod(Base):
    __tablename__ = "payment_methods"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(20))
    label: Mapped[str] = mapped_column(String(100))
    last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # Stripe PaymentMethod (pm_...) attached to the user's Customer; set on the real path.
    stripe_payment_method_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(20), nullable=True)
    exp_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exp_year: Mapped[int | None] = mapped_column(Integer, nullable=True)


class PayoutMethod(Base):
    __tablename__ = "payout_methods"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(20))
    label: Mapped[str] = mapped_column(String(100))
    last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    account_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # Stripe Connect linkage for bank payouts. `payouts_enabled` mirrors the connected
    # account's status once onboarding (details_submitted + payouts_enabled) completes.
    stripe_external_account_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payouts_enabled: Mapped[bool] = mapped_column(Boolean, default=False)


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    intent_alerts: Mapped[bool] = mapped_column(Boolean, default=True)
    chat_messages: Mapped[bool] = mapped_column(Boolean, default=True)
    review_results: Mapped[bool] = mapped_column(Boolean, default=True)
    marketing: Mapped[bool] = mapped_column(Boolean, default=False)
    remind_pay: Mapped[bool] = mapped_column(Boolean, default=True)
    remind_ship: Mapped[bool] = mapped_column(Boolean, default=True)
    remind_receive: Mapped[bool] = mapped_column(Boolean, default=True)
    remind_dispute: Mapped[bool] = mapped_column(Boolean, default=True)
    find_by_phone: Mapped[bool] = mapped_column(Boolean, default=True)
    show_wechat_badge: Mapped[bool] = mapped_column(Boolean, default=False)
    personalization: Mapped[bool] = mapped_column(Boolean, default=True)


class DevicePushToken(Base):
    __tablename__ = "device_push_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    platform: Mapped[str] = mapped_column(String(20))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SafetyReport(Base):
    __tablename__ = "safety_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    reporter_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    target_type: Mapped[str] = mapped_column(String(20))
    target_id: Mapped[str] = mapped_column(String(50))
    reason: Mapped[str] = mapped_column(String(100))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_urls_json: Mapped[str] = mapped_column(Text, default="[]")
    handler_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    handled_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def evidence_urls(self) -> list[str]:
        try:
            parsed = json.loads(self.evidence_urls_json)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []

    @evidence_urls.setter
    def evidence_urls(self, value: list[str]) -> None:
        self.evidence_urls_json = json.dumps(value)


class BlocklistEntry(Base):
    __tablename__ = "blocklist"
    __table_args__ = (UniqueConstraint("blocker_id", "blocked_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    blocker_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    blocked_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SystemNotification(Base):
    __tablename__ = "system_notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    category: Mapped[str] = mapped_column(String(20), default="system", index=True)
    title: Mapped[str] = mapped_column(String(200))
    title_zh: Mapped[str | None] = mapped_column(String(200), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    body_zh: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    action_ref: Mapped[str | None] = mapped_column(String(50), nullable=True)
    unread: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (UniqueConstraint("order_id", "reviewer_id", name="uq_reviews_order_reviewer"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    rating: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    communication_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expertise_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    professionalism_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hire_again_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Moderation: hide keeps the review in the DB but pulls it from public view;
    # removed is a soft-delete for violating content. Admin note records the reason.
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    is_removed: Mapped[bool] = mapped_column(Boolean, default=False)
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VerificationSubmission(Base):
    __tablename__ = "verification_submissions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    legal_name: Mapped[str] = mapped_column(String(100))
    id_country: Mapped[str] = mapped_column(String(2), default="AU")
    id_front_url: Mapped[str] = mapped_column(String(500))
    id_back_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    business_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    business_reg_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    abn: Mapped[str | None] = mapped_column(String(20), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship()


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    admin_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(50))
    target_type: Mapped[str] = mapped_column(String(30))
    target_id: Mapped[str] = mapped_column(String(50))
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BlockedKeyword(Base):
    __tablename__ = "blocked_keywords"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pattern: Mapped[str] = mapped_column(String(200), unique=True)
    locale: Mapped[str] = mapped_column(String(5), default="all")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class PlatformCategory(Base):
    __tablename__ = "platform_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(20), index=True)
    key: Mapped[str] = mapped_column(String(50), unique=True)
    label_en: Mapped[str] = mapped_column(String(100))
    label_zh: Mapped[str] = mapped_column(String(100))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Category icon key (mobile renders it) and whether the category appears on the home grid.
    icon: Mapped[str | None] = mapped_column(String(50), nullable=True)
    show_on_home: Mapped[bool] = mapped_column(Boolean, default=True)


class PlatformBanner(Base):
    __tablename__ = "platform_banners"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    title: Mapped[str] = mapped_column(String(200))
    image_url: Mapped[str] = mapped_column(String(500))
    link_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    position: Mapped[str] = mapped_column(String(20), default="home")
    online_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    offline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlatformTopic(Base):
    """专题 (topic zone) — a curated feature area, e.g. graduation clearance. Distinct from banners."""

    __tablename__ = "platform_topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200))
    title_zh: Mapped[str | None] = mapped_column(String(200), nullable=True)
    subtitle: Mapped[str | None] = mapped_column(String(300), nullable=True)
    cover_image_url: Mapped[str] = mapped_column(String(500), default="")
    # Optional filter that drives the zone's contents (e.g. a product tag key like "graduation").
    tag_key: Mapped[str | None] = mapped_column(String(50), nullable=True)
    link_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    online_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    offline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlatformRegion(Base):
    __tablename__ = "platform_regions"
    __table_args__ = (UniqueConstraint("country", "state", "city", "area", name="uq_platform_region"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    country: Mapped[str] = mapped_column(String(2), default="AU")
    state: Mapped[str] = mapped_column(String(10), index=True)
    city: Mapped[str] = mapped_column(String(50), index=True)
    area: Mapped[str | None] = mapped_column(String(50), nullable=True)
    label_en: Mapped[str] = mapped_column(String(100))
    label_zh: Mapped[str] = mapped_column(String(100))
    is_default_city: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class PromotionClickEvent(Base):
    __tablename__ = "promotion_click_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DailyActiveUser(Base):
    __tablename__ = "daily_active_users"
    __table_args__ = (UniqueConstraint("day", name="uq_daily_active_users_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day: Mapped[str] = mapped_column(String(10), index=True)
    user_count: Mapped[int] = mapped_column(Integer, default=0)


class DailyActiveUserHit(Base):
    __tablename__ = "daily_active_user_hits"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_dau_user_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    day: Mapped[str] = mapped_column(String(10), index=True)


class ReportReason(Base):
    """Admin-configurable report reasons (举报原因配置) shared by mobile report sheet + web."""

    __tablename__ = "report_reasons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(50), unique=True)
    label_en: Mapped[str] = mapped_column(String(100))
    label_zh: Mapped[str] = mapped_column(String(100))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ProductTag(Base):
    """Admin-configurable listing tags (商品标签) selectable on a listing."""

    __tablename__ = "product_tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(50), unique=True)
    label_en: Mapped[str] = mapped_column(String(100))
    label_zh: Mapped[str] = mapped_column(String(100))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class SearchLog(Base):
    """One row per search submitted, aggregated into 热门搜索词 (popular search terms)."""

    __tablename__ = "search_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    term: Mapped[str] = mapped_column(String(120), index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlatformSetting(Base):
    """Generic key/value store for 系统配置: home-module switches, user agreement, privacy policy."""

    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
