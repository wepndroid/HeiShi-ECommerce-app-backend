from datetime import datetime, timezone

from app.avatar_photos import avatar_url_for_user_id
from app.models import (
    Address,
    Conversation,
    Coupon,
    Follow,
    Listing,
    Message,
    Order,
    PaymentMethod,
    PayoutMethod,
    Review,
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
    PublicUserProfileDto,
    ReviewSummaryDto,
    SellerDto,
    SystemNotificationDto,
    InboxNotificationDto,
    NotificationGroupDto,
    VerificationStatusDto,
)


def iso(dt: datetime | None) -> str:
    if dt is None:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _user_avatar_url(user: User) -> str | None:
    return user.avatar_url or avatar_url_for_user_id(user.id)


def user_to_dto(user: User) -> AuthUserDto:
    lang = user.language if user.language in ("en", "zh") else "en"
    return AuthUserDto(
        id=user.id,
        nickname=user.nickname,
        phone=user.phone,
        avatarUrl=_user_avatar_url(user),
        bio=user.bio,
        city=user.city,
        language=lang,
        heishiId=user.heishi_id,
    )


def public_user_profile(
    user: User,
    *,
    rating: float,
    review_count: int,
    listing_count: int,
    follower_count: int,
    settings: UserSettings | None,
    lang: str = "en",
) -> PublicUserProfileDto:
    nickname = user.nickname
    if lang == "zh":
        nickname = _SELLER_NICKNAME_ZH.get(user.id, nickname)
    show_wechat = settings.show_wechat_badge if settings else False
    return PublicUserProfileDto(
        id=user.id,
        nickname=nickname,
        avatarUrl=_user_avatar_url(user),
        bio=user.bio,
        city=user.city,
        memberSince=iso(user.created_at),
        rating=round(rating, 1),
        reviewCount=review_count,
        listingCount=listing_count,
        followerCount=follower_count,
        phoneVerified=user.phone_verified,
        identityVerified=user.identity_verified,
        businessVerified=user.business_verified,
        wechatLinked=bool(user.wechat_bound and show_wechat),
        alipayLinked=user.alipay_bound,
    )


def seller_to_dto(user: User, lang: str = "en") -> SellerDto:
    nickname = user.nickname
    if lang == "zh":
        nickname = _SELLER_NICKNAME_ZH.get(user.id, nickname)
    return SellerDto(
        id=user.id,
        nickname=nickname,
        avatarUrl=_user_avatar_url(user),
        verified=user.identity_verified or user.business_verified,
    )


_SELLER_NICKNAME_ZH: dict[str, str] = {
    "seller-mia": "Mia_墨尔本",
    "seller-sunny": "阳光卖家",
    "seller-lucas": "Lucas_墨尔本",
    "seller-xiaoyu": "小雨同学",
    "seller-amy": "艾米",
    "seller-ticketShop": "票券小铺",
    "seller-pte": "PTE学长",
    "seller-luna": "露娜",
    "seller-coffee": "咖啡不加糖",
    "seller-allen": "艾伦",
    "seller-lily": "莉莉",
}


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
        seller=seller_to_dto(listing.seller, lang),
        status=status,
        createdAt=iso(listing.created_at),
    )


def listing_to_detail(listing: Listing, lang: str = "en") -> ListingDetailDto:
    summary = listing_to_summary(listing, lang)
    bundle_meta = listing.bundle_meta if listing.type == "bundle" and listing.bundle_meta else None
    return ListingDetailDto(
        **summary.model_dump(),
        images=listing.images,
        conditionKey=listing.condition_key,
        negotiable=listing.negotiable,
        escrowSupported=listing.escrow_supported,
        pickupMethods=listing.pickup_methods,
        viewCount=listing.view_count,
        favoriteCount=listing.favorite_count,
        bundleMeta=bundle_meta if bundle_meta else None,
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
        imageUrl=listing.image_url,
        seller=seller_to_dto(listing.seller, lang),
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
        seller=seller_to_dto(order.seller, lang),
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


def conversation_to_dto(conv: Conversation, current_user_id: str, lang: str = "en") -> ConversationDto:
    is_buyer = conv.buyer_id == current_user_id
    counterpart_user = conv.seller if is_buyer else conv.buyer
    unread = conv.buyer_unread if is_buyer else conv.seller_unread
    last_msg = None
    if conv.last_message_text and conv.last_message_at:
        last_msg = LastMessageDto(text=conv.last_message_text, sentAt=iso(conv.last_message_at))
    listing_ref = ListingRefDto(
        id=conv.listing.id,
        title=listing_title(conv.listing, lang),
        imageUrl=conv.listing.image_url,
        price=conv.listing.price,
        locationLabel=conv.listing.location_label,
    )
    return ConversationDto(
        id=conv.id,
        counterpart=CounterpartDto(
            id=counterpart_user.id,
            nickname=counterpart_user.nickname,
            avatarUrl=_user_avatar_url(counterpart_user),
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


def inbox_notification_to_dto(n: SystemNotification, lang: str = "en") -> InboxNotificationDto:
    title = n.title_zh if lang == "zh" and n.title_zh else n.title
    body = n.body_zh if lang == "zh" and n.body_zh else n.body
    category = n.category if n.category in ("system", "order", "follow") else "system"
    return InboxNotificationDto(
        id=n.id,
        category=category,
        title=title,
        body=body,
        createdAt=iso(n.created_at),
        unread=bool(n.unread),
        actionType=n.action_type,
        actionRef=n.action_ref,
    )


def notification_group_to_dto(
    category: str,
    unread_count: int,
    latest: SystemNotification | None,
    lang: str = "en",
) -> NotificationGroupDto:
    if latest:
        preview_title = latest.title_zh if lang == "zh" and latest.title_zh else latest.title
        preview_body = latest.body_zh if lang == "zh" and latest.body_zh else latest.body
        last_at = iso(latest.created_at)
    else:
        preview_title = ""
        preview_body = ""
        last_at = None
    return NotificationGroupDto(
        category=category if category in ("system", "order", "follow") else "system",
        unreadCount=unread_count,
        previewTitle=preview_title,
        previewBody=preview_body,
        lastAt=last_at,
    )
