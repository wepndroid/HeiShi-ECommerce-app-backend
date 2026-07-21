from datetime import datetime, timezone

from app.avatar_photos import avatar_url_for_user_id
from app.media_urls import normalize_media_url, normalize_media_urls
from app.messaging_read import marked_as_unread, message_ack_read
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
    ReceivedReviewDto,
    PendingReviewOrderDto,
    ReviewCriteriaDto,
    SellerDto,
    SystemNotificationDto,
    InboxNotificationDto,
    NotificationGroupDto,
    TransactionReminderSettingsDto,
    VerificationStatusDto,
)


def iso(dt: datetime | None) -> str:
    if dt is None:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _user_avatar_url(user: User) -> str | None:
    """Return the user's uploaded avatar only — no stock-photo fallback for real accounts."""
    if user.avatar_url:
        return normalize_media_url(user.avatar_url)
    return avatar_url_for_user_id(user.id)


def user_to_dto(user: User) -> AuthUserDto:
    lang = user.language if user.language in ("en", "zh") else "en"
    return AuthUserDto(
        id=user.id,
        nickname=user.nickname,
        phone=user.phone,
        email=getattr(user, "email", None),
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


def seller_to_dto(
    user: User,
    lang: str = "en",
    *,
    completed_order_count: int | None = None,
    positive_rating_rate: int | None = None,
) -> SellerDto:
    nickname = user.nickname
    if lang == "zh":
        nickname = _SELLER_NICKNAME_ZH.get(user.id, nickname)
    return SellerDto(
        id=user.id,
        nickname=nickname,
        avatarUrl=_user_avatar_url(user),
        verified=user.identity_verified or user.business_verified,
        phoneVerified=user.phone_verified,
        identityVerified=user.identity_verified,
        completedOrderCount=completed_order_count,
        positiveRatingRate=positive_rating_rate,
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


def listing_card_image_url(listing: Listing) -> str:
    """Use the optimized thumbnail for compact surfaces, never detail media."""
    thumbnail = normalize_media_url(getattr(listing, "thumbnail_url", None))
    if thumbnail:
        return thumbnail
    images = normalize_media_urls(listing.images)
    if images:
        return images[0]
    return normalize_media_url(listing.image_url) or ""


def listing_to_summary(
    listing: Listing,
    lang: str = "en",
    *,
    completed_order_count: int | None = None,
    positive_rating_rate: int | None = None,
) -> ListingSummaryDto:
    listing_type = listing.type if listing.type in ("product", "service", "bundle", "job", "rental") else "product"
    status = listing.status if listing.status in ("active", "draft", "sold", "inactive") else "active"
    review_status = listing.review_status if listing.review_status in ("pendingReview", "approved", "rejected", "removed", "draft") else "approved"
    images = normalize_media_urls(listing.images)
    videos = normalize_media_urls(listing.videos)
    cover = listing_card_image_url(listing)
    return ListingSummaryDto(
        id=listing.id,
        type=listing_type,
        title=listing_title(listing, lang),
        description=listing_description(listing, lang),
        price=listing.price,
        categoryKey=listing.category_key,
        tagKey=listing.tag_key,
        locationLabel=listing.location_label,
        imageUrl=cover or "",
        images=images,
        videos=videos,
        seller=seller_to_dto(
            listing.seller,
            lang,
            completed_order_count=completed_order_count,
            positive_rating_rate=positive_rating_rate,
        ),
        status=status,
        reviewStatus=review_status,
        reviewNote=listing.review_note,
        createdAt=iso(listing.created_at),
        favoriteCount=listing.favorite_count or 0,
        isPinned=listing.is_pinned,
        isRecommended=listing.is_recommended,
    )


def _infer_bundle_cover_urls(listing_images: list[str], items: list[dict]) -> list[str]:
    listing = normalize_media_urls(listing_images or [])
    if not listing:
        return []
    item_photos: list[str] = []
    for item in items:
        urls = list(item.get("imageUrls") or [])
        if not urls and item.get("imageUrl"):
            urls = [item["imageUrl"]]
        item_photos.extend(normalize_media_urls(urls))
    if not item_photos:
        return listing
    item_set = set(item_photos)
    exclusive = [url for url in listing if url not in item_set]
    if len(exclusive) == len(listing):
        return listing
    naive_len = max(len(listing) - len(item_photos), 1)
    covers = list(listing[:naive_len])
    if (
        len(covers) < len(listing)
        and listing[len(covers)] in item_set
        and listing[len(covers)] not in covers
    ):
        covers.append(listing[len(covers)])
    if covers:
        return covers
    return exclusive if exclusive else [listing[0]]


def listing_to_detail(listing: Listing, lang: str = "en", *, escrow_fee: float = 0.0) -> ListingDetailDto:
    summary = listing_to_summary(listing, lang)
    bundle_meta = None
    if listing.type == "bundle":
        raw_meta = listing.bundle_meta or {}
        if isinstance(raw_meta, dict) and raw_meta:
            bundle_meta = dict(raw_meta)
            items = bundle_meta.get("items") or []
            listing_images = normalize_media_urls(listing.images or [])
            if not bundle_meta.get("coverImageUrls"):
                if items:
                    bundle_meta["coverImageUrls"] = _infer_bundle_cover_urls(listing_images, items)
                elif listing_images:
                    bundle_meta["coverImageUrls"] = listing_images
    return ListingDetailDto(
        **summary.model_dump(),
        conditionKey=listing.condition_key,
        negotiable=listing.negotiable,
        escrowSupported=listing.escrow_supported,
        meetInPublic=listing.meet_in_public,
        pickupMethods=listing.pickup_methods,
        escrowFee=escrow_fee if listing.escrow_supported else 0.0,
        viewCount=listing.view_count,
        bundleMeta=bundle_meta if bundle_meta else None,
        serviceIcon=listing.service_icon if listing.type == "service" else None,
    )


def listing_to_service(listing: Listing, lang: str = "en") -> LocalServiceDto:
    icon = listing.service_icon if listing.service_icon in ("truck", "broom", "cameraService") else "truck"
    cover = listing_card_image_url(listing)
    return LocalServiceDto(
        id=listing.id,
        title=listing_title(listing, lang),
        description=listing_description(listing, lang) or "",
        priceFrom=listing.price,
        area=listing.location_label,
        icon=icon,
        imageUrl=cover or "",
        seller=seller_to_dto(listing.seller, lang),
    )


def order_to_dto(
    order: Order,
    lang: str = "en",
    *,
    include_buyer: bool = False,
    viewer_has_reviewed: bool = False,
) -> OrderDto:
    status = order.status if order.status in (
        "pendingPay",
        "pendingShip",
        "pendingService",
        "pendingReceive",
        "pendingReview",
        "completed",
        "cancelled",
        "refunded",
        "inDispute",
        "refundInProgress",
    ) else "pendingPay"
    buyer = seller_to_dto(order.buyer, lang) if include_buyer and order.buyer else None
    return OrderDto(
        id=order.id,
        listingId=order.listing_id,
        listingTitle=listing_title(order.listing, lang),
        listingImageUrl=listing_card_image_url(order.listing),
        seller=seller_to_dto(order.seller, lang),
        buyer=buyer,
        status=status,
        amount=order.amount,
        escrowFee=order.escrow_fee,
        displayAmountCny=order.display_amount_cny,
        deliveryMethod=order.delivery_method,
        paymentMethodId=order.payment_method_id,
        bundleItemId=order.bundle_item_id,
        privateOfferId=order.private_offer_id,
        couponId=order.coupon_id,
        discountAmount=order.discount_amount if order.discount_amount else None,
        createdAt=iso(order.created_at),
        updatedAt=iso(order.updated_at),
        viewerHasReviewed=viewer_has_reviewed,
    )


def favorite_to_dto(listing_id: int, created_at: datetime) -> FavoriteDto:
    return FavoriteDto(listingId=listing_id, createdAt=iso(created_at))


def follow_to_dto(user: User, followed_at: datetime, lang: str = "en") -> FollowDto:
    nickname = user.nickname
    if lang == "zh":
        nickname = _SELLER_NICKNAME_ZH.get(user.id, nickname)
    return FollowDto(
        userId=user.id,
        nickname=nickname,
        subtitle=user.city,
        avatarUrl=_user_avatar_url(user),
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
    listing_cover = listing_card_image_url(conv.listing)
    listing_ref = ListingRefDto(
        id=conv.listing.id,
        title=listing_title(conv.listing, lang),
        imageUrl=listing_cover or "",
        price=conv.listing.price,
        locationLabel=conv.listing.location_label,
        status=conv.listing.status if conv.listing.status in ("active", "draft", "sold", "inactive") else "active",
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
        markedAsUnread=marked_as_unread(conv, current_user_id),
    )


def message_to_dto(msg: Message, conv: Conversation | None = None, viewer_id: str | None = None) -> ChatMessageDto:
    ack_read = False
    if conv is not None and viewer_id is not None:
        ack_read = message_ack_read(conv, msg, viewer_id)
    price = None
    kind = "text"
    if msg.text.startswith("__PRICE_CHANGE__:"):
        try:
            price = float(msg.text.split(":", 1)[1])
            kind = "priceChange"
        except (TypeError, ValueError):
            price = None
    structured_payload = None
    if msg.message_type == "private_offer":
        kind = "privateOffer"
        try:
            parsed = json.loads(msg.structured_payload_json or "{}")
            structured_payload = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            structured_payload = None
    text = f"Price updated to A${price:.2f}" if price is not None else msg.text
    return ChatMessageDto(
        id=msg.id,
        conversationId=msg.conversation_id,
        senderId=msg.sender_id,
        text=text,
        sentAt=iso(msg.sent_at),
        ackRead=ack_read,
        kind=kind,
        price=price,
        structuredPayload=structured_payload,
        officialPlatformMessage=bool(msg.official_platform_message),
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
    allowed = ("card", "apple_pay", "google_pay", "alipay", "wechat_pay", "paypal")
    pm_type = pm.type if pm.type in allowed else "card"
    return PaymentMethodDto(
        id=pm.id,
        type=pm_type,
        label=pm.label,
        last4=pm.last4,
        brand=getattr(pm, "brand", None),
        expMonth=getattr(pm, "exp_month", None),
        expYear=getattr(pm, "exp_year", None),
        isDefault=pm.is_default,
    )


def payout_to_dto(pm: PayoutMethod) -> PayoutMethodDto:
    allowed = ("bank", "paypal", "alipay", "wechat")
    pm_type = pm.type if pm.type in allowed else "bank"
    label = pm.label
    account_hint: str | None = None
    raw_ref = getattr(pm, "account_ref", None)
    if pm_type == "bank":
        label = "Australian bank account"
        account_hint = f"**** {pm.last4}" if pm.last4 else None
    elif raw_ref:
        account_hint = _mask_account_hint(pm_type, raw_ref)
    return PayoutMethodDto(
        id=pm.id,
        type=pm_type,
        label=label,
        last4=pm.last4,
        accountHint=account_hint,
        payoutsEnabled=getattr(pm, "payouts_enabled", None),
        isDefault=pm.is_default,
    )


def _mask_account_hint(method_type: str, value: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        return ""
    if method_type == "paypal" and "@" in trimmed:
        local, domain = trimmed.split("@", 1)
        if len(local) <= 2:
            masked_local = local[:1] + "***"
        else:
            masked_local = local[:2] + "***" + local[-1]
        return f"{masked_local}@{domain}"
    if len(trimmed) <= 4:
        return trimmed[0] + "***"
    return f"{trimmed[:2]}***{trimmed[-4:]}"


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


def settings_to_transaction_reminders(s: UserSettings) -> TransactionReminderSettingsDto:
    return TransactionReminderSettingsDto(
        payAlerts=s.remind_pay,
        shipAlerts=s.remind_ship,
        receiveAlerts=s.remind_receive,
        disputeAlerts=s.remind_dispute,
    )


def verification_to_dto(user: User, *, submission_status: str = "not_submitted") -> VerificationStatusDto:
    status = submission_status if submission_status in ("not_submitted", "pending", "approved", "rejected") else "not_submitted"
    return VerificationStatusDto(
        phoneVerified=user.phone_verified,
        wechatBound=user.wechat_bound,
        alipayBound=user.alipay_bound,
        identityVerified=user.identity_verified,
        businessVerified=user.business_verified,
        submissionStatus=status,
    )


def credit_profile(user_id: str, db_orders: int, rating: float, completion_rate: float) -> CreditProfileDto:
    return CreditProfileDto(
        score=min(850, 600 + db_orders * 10),
        trades=db_orders,
        completionRate=completion_rate,
        violations=0,
        rating=rating,
    )


def review_summary(
    seller_rating: float,
    pending: int,
    seller_received_count: int,
    *,
    buyer_rating: float = 0.0,
    buyer_received_count: int = 0,
) -> ReviewSummaryDto:
    return ReviewSummaryDto(
        score=seller_rating,
        pendingCount=pending,
        receivedCount=seller_received_count,
        buyerScore=buyer_rating,
        buyerReceivedCount=buyer_received_count,
    )


def _review_criteria_dto(review: Review) -> ReviewCriteriaDto | None:
    if review.quality_rating is None:
        return None
    return ReviewCriteriaDto(
        quality=review.quality_rating,
        communication=review.communication_rating or review.quality_rating,
        trustement=review.expertise_rating or review.quality_rating,
    )


def received_review_to_dto(
    review: Review,
    order: Order,
    listing: Listing,
    reviewer: User,
) -> ReceivedReviewDto:
    reviewer_role = "buyer" if review.reviewer_id == order.buyer_id else "seller"
    return ReceivedReviewDto(
        id=review.id,
        orderId=order.id,
        rating=review.rating,
        comment=review.comment,
        criteria=_review_criteria_dto(review),
        createdAt=iso(review.created_at),
        listingTitle=listing.title,
        listingImageUrl=listing_card_image_url(listing),
        listingId=listing.id,
        reviewerNickname=reviewer.nickname,
        reviewerRole=reviewer_role,
    )


def pending_review_order_to_dto(
    order: Order,
    lang: str,
    *,
    review_role: str,
    counterpart_nickname: str,
) -> PendingReviewOrderDto:
    return PendingReviewOrderDto(
        orderId=order.id,
        listingId=order.listing_id,
        listingTitle=listing_title(order.listing, lang),
        listingImageUrl=listing_card_image_url(order.listing),
        amount=order.amount,
        counterpartNickname=counterpart_nickname,
        reviewRole=review_role,
    )


def system_notification_to_dto(n: SystemNotification) -> SystemNotificationDto:
    return SystemNotificationDto(
        id=n.id,
        notificationId=n.id,
        userId=n.user_id,
        userRoleContext=n.user_role_context,
        notificationCategory=n.notification_category or n.category,
        notificationType=n.notification_type,
        title=n.title,
        body=n.body,
        content=n.body,
        businessType=n.business_type,
        businessId=n.business_id,
        deepLink=n.deep_link,
        pushStatus=n.push_status,
        readStatus="unread" if n.unread else "read",
        createdAt=iso(n.created_at),
        readAt=iso(n.read_at) if n.read_at else None,
        unread=n.unread,
    )


def inbox_notification_to_dto(n: SystemNotification, lang: str = "en") -> InboxNotificationDto:
    title = n.title_zh if lang == "zh" and n.title_zh else n.title
    body = n.body_zh if lang == "zh" and n.body_zh else n.body
    category = n.category if n.category in ("system", "order", "follow") else "system"
    return InboxNotificationDto(
        id=n.id,
        notificationId=n.id,
        userId=n.user_id,
        userRoleContext=n.user_role_context,
        notificationCategory=n.notification_category or n.category,
        notificationType=n.notification_type,
        category=category,
        title=title,
        body=body,
        content=body,
        businessType=n.business_type,
        businessId=n.business_id,
        deepLink=n.deep_link,
        pushStatus=n.push_status,
        readStatus="unread" if n.unread else "read",
        createdAt=iso(n.created_at),
        readAt=iso(n.read_at) if n.read_at else None,
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
