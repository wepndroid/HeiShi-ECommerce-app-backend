from datetime import datetime, timezone

from app.models import (
    Address,
    Conversation,
    Coupon,
    Listing,
    Message,
    Order,
    PaymentMethod,
    PayoutMethod,
    SystemNotification,
    User,
    UserSettings,
)
from app.schemas import (
    AddressDto,
    AuthUserDto,
    ChatMessageDto,
    ConversationDto,
    CounterpartDto,
    CouponDto,
    CreditProfileDto,
    FavoriteDto,
    FollowDto,
    LastMessageDto,
    ListingDetailDto,
    ListingRefDto,
    ListingSummaryDto,
    LocalServiceDto,
    NotificationSettingsDto,
    OrderDto,
    PaymentMethodDto,
    PayoutMethodDto,
    PrivacySettingsDto,
    ReviewSummaryDto,
    SellerDto,
    SystemNotificationDto,
    VerificationStatusDto,
)


def iso(dt: datetime | None) -> str:
    if dt is None:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def user_to_dto(user: User) -> AuthUserDto:
    lang = user.language if user.language in ("en", "zh") else "en"
    return AuthUserDto(
        id=user.id,
        nickname=user.nickname,
        phone=user.phone,
        avatarUrl=user.avatar_url,
        bio=user.bio,
        city=user.city,
        language=lang,
        heishiId=user.heishi_id,
    )


def seller_to_dto(user: User) -> SellerDto:
    return SellerDto(
        id=user.id,
        nickname=user.nickname,
        avatarUrl=user.avatar_url,
        verified=user.identity_verified or user.business_verified,
    )


def listing_title(listing: Listing, lang: str = "en") -> str:
    if lang == "zh" and listing.title_zh:
        return listing.title_zh
    return listing.title


def listing_description(listing: Listing, lang: str = "en") -> str | None:
    desc = listing.description_zh if lang == "zh" and listing.description_zh else listing.description
    return desc or None


def listing_to_summary(listing: Listing, lang: str = "en") -> ListingSummaryDto:
    listing_type = listing.type if listing.type in ("product", "service", "bundle") else "product"
    status = listing.status if listing.status in ("active", "draft", "sold", "inactive") else "active"
    return ListingSummaryDto(
        id=listing.id,
        type=listing_type,
        title=listing_title(listing, lang),
        description=listing_description(listing, lang),
        price=listing.price,
        categoryKey=listing.category_key,
        tagKey=listing.tag_key,
        locationLabel=listing.location_label,
        imageUrl=listing.image_url,
        seller=seller_to_dto(listing.seller),
        status=status,
        createdAt=iso(listing.created_at),
    )


def listing_to_detail(listing: Listing, lang: str = "en") -> ListingDetailDto:
    summary = listing_to_summary(listing, lang)
    return ListingDetailDto(
        **summary.model_dump(),
        images=listing.images,
        conditionKey=listing.condition_key,
        negotiable=listing.negotiable,
        escrowSupported=listing.escrow_supported,
        pickupMethods=listing.pickup_methods,
        viewCount=listing.view_count,
        favoriteCount=listing.favorite_count,
    )


def listing_to_service(listing: Listing, lang: str = "en") -> LocalServiceDto:
    icon = listing.service_icon if listing.service_icon in ("truck", "broom", "cameraService") else "truck"
    return LocalServiceDto(
        id=listing.id,
        title=listing_title(listing, lang),
        description=listing_description(listing, lang) or "",
        priceFrom=listing.price,
        area=listing.location_label,
        icon=icon,
        seller=seller_to_dto(listing.seller),
    )


def order_to_dto(order: Order, lang: str = "en") -> OrderDto:
    status = order.status if order.status in (
        "pendingPay", "pendingShip", "pendingReceive", "pendingReview", "completed", "cancelled"
    ) else "pendingPay"
    return OrderDto(
        id=order.id,
        listingId=order.listing_id,
        listingTitle=listing_title(order.listing, lang),
        listingImageUrl=order.listing.image_url,
        seller=seller_to_dto(order.seller),
        status=status,
        amount=order.amount,
        escrowFee=order.escrow_fee,
        createdAt=iso(order.created_at),
        updatedAt=iso(order.updated_at),
    )


def favorite_to_dto(listing_id: int, created_at: datetime) -> FavoriteDto:
    return FavoriteDto(listingId=listing_id, createdAt=iso(created_at))


def follow_to_dto(user: User, followed_at: datetime) -> FollowDto:
    return FollowDto(
        userId=user.id,
        nickname=user.nickname,
        subtitle=user.city,
        followedAt=iso(followed_at),
    )


def coupon_to_dto(coupon: Coupon) -> CouponDto:
    status = coupon.status if coupon.status in ("available", "used", "expired") else "available"
    return CouponDto(
        id=coupon.id,
        amount=coupon.amount,
        description=coupon.description,
        expiresAt=iso(coupon.expires_at) if coupon.expires_at else None,
        status=status,
    )


def conversation_to_dto(conv: Conversation, current_user_id: str) -> ConversationDto:
    is_buyer = conv.buyer_id == current_user_id
    counterpart_user = conv.seller if is_buyer else conv.buyer
    unread = conv.buyer_unread if is_buyer else conv.seller_unread
    last_msg = None
    if conv.last_message_text and conv.last_message_at:
        last_msg = LastMessageDto(text=conv.last_message_text, sentAt=iso(conv.last_message_at))
    listing_ref = ListingRefDto(
        id=conv.listing.id,
        title=conv.listing.title,
        imageUrl=conv.listing.image_url,
    )
    return ConversationDto(
        id=conv.id,
        counterpart=CounterpartDto(
            id=counterpart_user.id,
            nickname=counterpart_user.nickname,
            avatarUrl=counterpart_user.avatar_url,
        ),
        listing=listing_ref,
        lastMessage=last_msg,
        unreadCount=unread,
    )


def message_to_dto(msg: Message) -> ChatMessageDto:
    return ChatMessageDto(
        id=msg.id,
        conversationId=msg.conversation_id,
        senderId=msg.sender_id,
        text=msg.text,
        sentAt=iso(msg.sent_at),
    )


def address_to_dto(addr: Address) -> AddressDto:
    return AddressDto(
        id=addr.id,
        label=addr.label,
        area=addr.area,
        meetupSpot=addr.meetup_spot,
        isDefault=addr.is_default,
    )


def payment_to_dto(pm: PaymentMethod) -> PaymentMethodDto:
    pm_type = pm.type if pm.type in ("card", "apple_pay", "paypal") else "card"
    return PaymentMethodDto(id=pm.id, type=pm_type, label=pm.label, last4=pm.last4, isDefault=pm.is_default)


def payout_to_dto(pm: PayoutMethod) -> PayoutMethodDto:
    pm_type = pm.type if pm.type in ("bank", "paypal") else "bank"
    return PayoutMethodDto(id=pm.id, type=pm_type, label=pm.label, last4=pm.last4, isDefault=pm.is_default)


def settings_to_notification(s: UserSettings) -> NotificationSettingsDto:
    return NotificationSettingsDto(
        intentAlerts=s.intent_alerts,
        chatMessages=s.chat_messages,
        reviewResults=s.review_results,
        marketing=s.marketing,
    )


def settings_to_privacy(s: UserSettings) -> PrivacySettingsDto:
    return PrivacySettingsDto(
        findByPhone=s.find_by_phone,
        showWechatBadge=s.show_wechat_badge,
        personalization=s.personalization,
    )


def verification_to_dto(user: User) -> VerificationStatusDto:
    return VerificationStatusDto(
        phoneVerified=user.phone_verified,
        wechatBound=user.wechat_bound,
        alipayBound=user.alipay_bound,
        identityVerified=user.identity_verified,
        businessVerified=user.business_verified,
    )


def credit_profile(user_id: str, db_orders: int, rating: float) -> CreditProfileDto:
    return CreditProfileDto(
        score=min(850, 600 + db_orders * 10),
        trades=db_orders,
        completionRate=1.0 if db_orders == 0 else 0.95,
        violations=0,
        rating=rating,
    )


def review_summary(rating: float, pending: int) -> ReviewSummaryDto:
    return ReviewSummaryDto(score=rating, pendingCount=pending)


def system_notification_to_dto(n: SystemNotification) -> SystemNotificationDto:
    return SystemNotificationDto(
        id=n.id,
        title=n.title,
        body=n.body,
        createdAt=iso(n.created_at),
        unread=n.unread,
    )
