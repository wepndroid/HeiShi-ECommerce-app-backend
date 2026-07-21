from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import Query, Session, aliased

from app.models import (
    Conversation,
    ExposureRule,
    Favorite,
    Follow,
    FollowedCategory,
    Listing,
    Message,
    Order,
    PrivateOffer,
    PromotionClickEvent,
    Review,
    User,
    ViewHistory,
)

PENDING_PAY_STATUS = "pendingPay"

OTHER_AREAS = "其他地区"
ALL_AREAS = "全部区域"
ALL_AREAS_SENTINELS = {ALL_AREAS, OTHER_AREAS}


def normalize_location(loc: str) -> str:
    return "Melbourne CBD" if loc == "CBD" else loc


def listing_in_region(listing: Listing, region_state: str | None, region_city: str | None, region_area: str | None) -> bool:
    if region_state and listing.region_state != region_state:
        return False
    if region_city and listing.region_city != region_city:
        return False
    if not region_area or region_area in ALL_AREAS_SENTINELS:
        return True
    loc = normalize_location(listing.location_label)
    area = normalize_location(region_area)
    if area == OTHER_AREAS:
        known = {
            "Box Hill", "Glen Waverley", "Clayton", "Doncaster", "Melbourne CBD",
            "Southbank", "Carlton", "Burwood", "Docklands", "Richmond", "Online",
        }
        return loc not in known
    if area == "Melbourne CBD":
        return "CBD" in loc or "Melbourne" in loc
    return loc == area or area in loc or loc in area


def apply_region_filter(q: Query, region_state: str | None, region_city: str | None, region_area: str | None) -> Query:
    if region_state:
        q = q.filter(Listing.region_state == region_state)
    if region_city:
        q = q.filter(Listing.region_city == region_city)
    if region_area and region_area not in ALL_AREAS_SENTINELS:
        if region_area == OTHER_AREAS:
            known = [
                "Box Hill", "Glen Waverley", "Clayton", "Doncaster", "Melbourne CBD",
                "Southbank", "Carlton", "Burwood", "Docklands", "Richmond",
            ]
            q = q.filter(~Listing.location_label.in_(known))
        elif region_area == "Melbourne CBD":
            q = q.filter(or_(Listing.location_label.contains("CBD"), Listing.location_label.contains("Melbourne")))
        else:
            q = q.filter(
                or_(
                    Listing.location_label == region_area,
                    Listing.location_label.contains(region_area),
                    Listing.region_area == region_area,
                )
            )
    return q


def apply_tab_filter(q: Query, tab: str | None) -> Query:
    if not tab or tab == "recommended":
        return q
    if tab == "newArrivals":
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        return q.filter(Listing.created_at >= cutoff)
    if tab == "digital":
        return q.filter(Listing.category_key == "digital")
    if tab == "services":
        return q.filter(Listing.type == "service")
    if tab == "tickets":
        return q.filter(Listing.category_key == "tickets")
    if tab == "jobs":
        return q.filter(Listing.type == "job")
    if tab == "rentals":
        return q.filter(Listing.type == "rental")
    if tab == "secondhand":
        return q.filter(Listing.type.in_(("product", "bundle")))
    return q


def apply_feed_sort(
    q: Query,
    viewer_user_id: str | None = None,
    viewer_city: str | None = None,
) -> Query:
    """Apply active admin exposure rules, then the normal organic ranking.

    A correlated subquery avoids duplicate listings when more than one rule targets
    the same product. Suppression is server-enforced and expired rules have no effect.
    """
    now = datetime.now(timezone.utc)
    target_region_matches = (
        or_(
            ExposureRule.target_region.is_(None),
            ExposureRule.target_region == viewer_city,
        )
        if viewer_city
        else ExposureRule.target_region.is_(None)
    )
    target_matches = and_(
        target_region_matches,
        or_(
            ExposureRule.target_category.is_(None),
            ExposureRule.target_category == Listing.category_key,
        ),
    )
    active_rule = and_(
        ExposureRule.product_id == Listing.id,
        ExposureRule.status == "active",
        or_(ExposureRule.start_time.is_(None), ExposureRule.start_time <= now),
        or_(ExposureRule.end_time.is_(None), ExposureRule.end_time > now),
        target_matches,
    )
    excluded = exists(
        select(ExposureRule.id).where(
            active_rule,
            ExposureRule.rule_type == "exclude",
        )
    )
    pinned = exists(
        select(ExposureRule.id).where(active_rule, ExposureRule.rule_type == "pin")
    )
    positive_weight = (
        select(func.max(ExposureRule.exposure_weight))
        .where(
            active_rule,
            ExposureRule.rule_type.in_(("boost", "pin", "regional", "category")),
        )
        .correlate(Listing)
        .scalar_subquery()
    )
    suppress_weight = (
        select(func.min(ExposureRule.exposure_weight))
        .where(active_rule, ExposureRule.rule_type == "suppress")
        .correlate(Listing)
        .scalar_subquery()
    )
    conversation_count = (
        select(func.count(Conversation.id))
        .where(Conversation.listing_id == Listing.id)
        .correlate(Listing)
        .scalar_subquery()
    )
    completed_order_count = (
        select(func.count(Order.id))
        .where(Order.listing_id == Listing.id, Order.status.in_(("completed", "pendingReview")))
        .correlate(Listing)
        .scalar_subquery()
    )
    promotion_click_count = (
        select(func.count(PromotionClickEvent.id))
        .where(PromotionClickEvent.listing_id == Listing.id)
        .correlate(Listing)
        .scalar_subquery()
    )
    seller_rating = (
        select(func.avg(Review.rating))
        .join(Order, Review.order_id == Order.id)
        .where(Order.seller_id == Listing.seller_id, Review.is_hidden.is_(False), Review.is_removed.is_(False))
        .correlate(Listing)
        .scalar_subquery()
    )
    interest_score = 0.0
    if viewer_user_id:
        favorite_listing = aliased(Listing)
        viewed_listing = aliased(Listing)
        ordered_listing = aliased(Listing)
        conversation_listing = aliased(Listing)
        interest_score = (
            case(
                (
                    exists(
                        select(Favorite.id)
                        .join(
                            favorite_listing,
                            Favorite.listing_id == favorite_listing.id,
                        )
                        .where(
                            Favorite.user_id == viewer_user_id,
                            favorite_listing.category_key == Listing.category_key,
                        )
                    ),
                    1.5,
                ),
                else_=0.0,
            )
            + case(
                (
                    exists(
                        select(ViewHistory.id)
                        .join(
                            viewed_listing,
                            ViewHistory.listing_id == viewed_listing.id,
                        )
                        .where(
                            ViewHistory.user_id == viewer_user_id,
                            viewed_listing.category_key == Listing.category_key,
                        )
                    ),
                    1.0,
                ),
                else_=0.0,
            )
            + case(
                (
                    exists(
                        select(Order.id)
                        .join(
                            ordered_listing,
                            Order.listing_id == ordered_listing.id,
                        )
                        .where(
                            Order.buyer_id == viewer_user_id,
                            ordered_listing.category_key == Listing.category_key,
                            Order.status.in_(
                                (
                                    "pendingShip",
                                    "pendingService",
                                    "pendingReceive",
                                    "pendingReview",
                                    "completed",
                                )
                            ),
                        )
                    ),
                    2.0,
                ),
                else_=0.0,
            )
            + case(
                (
                    exists(
                        select(Conversation.id)
                        .join(
                            conversation_listing,
                            Conversation.listing_id == conversation_listing.id,
                        )
                        .where(
                            Conversation.buyer_id == viewer_user_id,
                            conversation_listing.category_key
                            == Listing.category_key,
                        )
                    ),
                    1.0,
                ),
                else_=0.0,
            )
            + case(
                (
                    exists(
                        select(Follow.id).where(
                            Follow.follower_id == viewer_user_id,
                            Follow.followed_id == Listing.seller_id,
                        )
                    ),
                    2.0,
                ),
                else_=0.0,
            )
            + case(
                (
                    exists(
                        select(FollowedCategory.id).where(
                            FollowedCategory.user_id == viewer_user_id,
                            FollowedCategory.category_key == Listing.category_key,
                        )
                    ),
                    2.0,
                ),
                else_=0.0,
            )
        )
    geographic_score = (
        case((Listing.region_city == viewer_city, 1.25), else_=0.0)
        if viewer_city
        else 0.0
    )
    product_quality_score = (
        case((Listing.thumbnail_url.is_not(None), 0.25), else_=0.0)
        + case((Listing.description.is_not(None), 0.15), else_=0.0)
    )
    view_denominator = func.coalesce(Listing.view_count, 0) + 1.0
    conversation_denominator = func.coalesce(conversation_count, 0) + 1.0
    organic_score = (
        # Use rates as quality signals so a high-volume listing does not rank
        # solely because it has existed longer. Raw activity remains a small
        # confidence signal for brand-new listings.
        (
            func.coalesce(Listing.favorite_count, 0)
            / view_denominator
        )
        * 2.0
        + (
            func.coalesce(conversation_count, 0)
            / view_denominator
        )
        * 2.0
        + (
            func.coalesce(completed_order_count, 0)
            / conversation_denominator
        )
        * 3.0
        + func.coalesce(Listing.view_count, 0) * 0.002
        + func.coalesce(seller_rating, 0) * 0.1
        # Promotion clicks divided by views provide a bounded click-through
        # quality signal. The +1 avoids division by zero for new listings.
        + (
            func.coalesce(promotion_click_count, 0)
            / view_denominator
        )
        * 0.5
        + interest_score
        + geographic_score
        + product_quality_score
    )
    # ``exclude`` removes a product. ``suppress`` only reduces its ranking and
    # therefore must remain visible, which is materially different behavior.
    q = q.filter(~excluded)
    return q.order_by(
        pinned.desc(),
        func.coalesce(suppress_weight, positive_weight, 1.0).desc(),
        Listing.is_pinned.desc(),
        Listing.is_recommended.desc(),
        organic_score.desc(),
        Listing.created_at.desc(),
    )


def listing_excluded_from_recommendations(
    db: Session,
    listing: Listing,
    viewer_region: str | None = None,
) -> bool:
    """Apply active recommendation exclusions outside SQL feed queries too."""
    now = datetime.now(timezone.utc)
    return (
        db.query(ExposureRule.id)
        .filter(
            ExposureRule.product_id == listing.id,
            ExposureRule.rule_type == "exclude",
            ExposureRule.status == "active",
            or_(ExposureRule.start_time.is_(None), ExposureRule.start_time <= now),
            or_(ExposureRule.end_time.is_(None), ExposureRule.end_time > now),
            (
                or_(
                    ExposureRule.target_region.is_(None),
                    ExposureRule.target_region == viewer_region,
                )
                if viewer_region
                else ExposureRule.target_region.is_(None)
            ),
            or_(
                ExposureRule.target_category.is_(None),
                ExposureRule.target_category == listing.category_key,
            ),
        )
        .first()
        is not None
    )


def exclude_unpaid_reserved(q: Query, db: Session, viewer_user_id: str | None = None) -> Query:
    """Keep unpaid checkout attempts visible; successful payment owns inventory."""
    return q


def apply_feed_listing_status_filter(q: Query, db: Session, user_id: str | None) -> Query:
    """General discovery feeds contain only approved, currently purchasable listings.

    Transaction participants can still open sold/inactive listings through their order,
    chat, review, and sales-history routes.  Those records must not leak back into Home,
    Search, Related, or category feeds merely because the viewer participated in an order
    or owns the listing.
    """
    return q.filter(
        Listing.status == "active",
        Listing.review_status == "approved",
        Listing.seller.has(User.account_status == "normal"),
    )


def apply_public_listing_visibility_filter(q: Query) -> Query:
    """Public seller/profile pages and direct public detail views."""
    return q.filter(
        Listing.status == "active",
        Listing.review_status == "approved",
        Listing.seller.has(User.account_status == "normal"),
    )


def apply_search(q: Query, q_text: str | None, sort: str | None) -> Query:
    if q_text:
        pattern = f"%{q_text.strip()}%"
        q = q.filter(or_(Listing.title.ilike(pattern), Listing.description.ilike(pattern), Listing.title_zh.ilike(pattern), Listing.description_zh.ilike(pattern)))
    if sort == "priceAsc":
        q = q.order_by(Listing.price.asc())
    elif sort == "priceDesc":
        q = q.order_by(Listing.price.desc())
    elif sort == "newest":
        q = q.order_by(Listing.created_at.desc())
    elif sort == "relevance" and q_text:
        q = q.order_by(Listing.is_pinned.desc(), Listing.is_recommended.desc(), Listing.view_count.desc(), Listing.created_at.desc())
    else:
        q = q.order_by(Listing.is_pinned.desc(), Listing.is_recommended.desc(), Listing.created_at.desc())
    return q


def listing_checkout_amount(listing: Listing) -> float:
    """Checkout price; bundle listings exclude shares of sold items."""
    if listing.type == "bundle" and isinstance(listing.bundle_meta, dict):
        meta = listing.bundle_meta
        try:
            full_price = float(meta.get("fullPrice", listing.price))
        except (TypeError, ValueError):
            full_price = float(listing.price)
        sold_share = 0.0
        for item in meta.get("items") or []:
            if isinstance(item, dict) and item.get("status") in ("sold", "onHold"):
                try:
                    sold_share += float(item.get("sharePrice") or 0)
                except (TypeError, ValueError):
                    pass
        return max(full_price - sold_share, 0.0)
    return float(listing.price)


def reset_bundle_meta_for_resale(raw: dict | None) -> dict | None:
    if not raw or not isinstance(raw, dict):
        return None
    meta = dict(raw)
    items = []
    for item in meta.get("items") or []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["status"] = "available"
        items.append(row)
    meta["items"] = items
    return meta


def apply_bundle_payment(listing: Listing) -> None:
    """Mark bundle items sold after full remaining-bundle payment."""
    if listing.type != "bundle" or not isinstance(listing.bundle_meta, dict):
        listing.status = "sold"
        return
    meta = dict(listing.bundle_meta)
    items = []
    for item in meta.get("items") or []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        if row.get("status") != "sold":
            row["status"] = "sold"
        items.append(row)
    meta["items"] = items
    listing.bundle_meta = meta
    listing.status = "sold"


def find_bundle_item(listing: Listing, bundle_item_id: str) -> dict | None:
    if listing.type != "bundle" or not isinstance(listing.bundle_meta, dict):
        return None
    for item in listing.bundle_meta.get("items") or []:
        if isinstance(item, dict) and item.get("id") == bundle_item_id:
            return item
    return None


def bundle_allows_separate_sale(listing: Listing) -> bool:
    if listing.type != "bundle" or not isinstance(listing.bundle_meta, dict):
        return False
    return listing.bundle_meta.get("allowSeparateSale", True) is not False


def bundle_item_separate_price(item: dict) -> float:
    try:
        val = float(item.get("separatePrice") or 0)
    except (TypeError, ValueError):
        val = 0.0
    return val if val > 0 else 0.0


def bundle_item_is_available(item: dict) -> bool:
    return item.get("status", "available") == "available"


def _write_bundle_meta_items(listing: Listing, items: list) -> None:
    meta = dict(listing.bundle_meta) if isinstance(listing.bundle_meta, dict) else {}
    meta["items"] = items
    listing.bundle_meta = meta


def set_bundle_item_status(listing: Listing, bundle_item_id: str, status: str) -> bool:
    if not isinstance(listing.bundle_meta, dict):
        return False
    items = []
    found = False
    for item in listing.bundle_meta.get("items") or []:
        if not isinstance(item, dict):
            items.append(item)
            continue
        row = dict(item)
        if row.get("id") == bundle_item_id:
            row["status"] = status
            found = True
        items.append(row)
    if not found:
        return False
    _write_bundle_meta_items(listing, items)
    return True


def all_bundle_items_sold(listing: Listing) -> bool:
    if not isinstance(listing.bundle_meta, dict):
        return True
    items = listing.bundle_meta.get("items") or []
    if not items:
        return True
    return all(isinstance(item, dict) and item.get("status") == "sold" for item in items)


def apply_bundle_item_payment(listing: Listing, bundle_item_id: str) -> None:
    set_bundle_item_status(listing, bundle_item_id, "sold")
    if all_bundle_items_sold(listing):
        listing.status = "sold"


def release_bundle_item_hold(listing: Listing, bundle_item_id: str) -> None:
    item = find_bundle_item(listing, bundle_item_id)
    if item and item.get("status") == "onHold":
        set_bundle_item_status(listing, bundle_item_id, "available")


def release_order_bundle_hold(db: Session, order: Order) -> None:
    if not order.bundle_item_id:
        return
    listing = db.query(Listing).filter(Listing.id == order.listing_id).first()
    if listing:
        release_bundle_item_hold(listing, order.bundle_item_id)


def expire_stale_pending_pay_orders(db: Session, ttl_minutes: int = 30) -> None:
    """Cancel unpaid orders past TTL so listings are released back to catalog."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes)
    stale = (
        db.query(Order)
        .filter(Order.status == PENDING_PAY_STATUS, Order.created_at < cutoff)
        .all()
    )
    if not stale:
        return
    now = datetime.now(timezone.utc)
    for order in stale:
        release_order_bundle_hold(db, order)
        if order.private_offer_id:
            offer = db.query(PrivateOffer).filter(PrivateOffer.id == order.private_offer_id).first()
            if offer and offer.status == "CONVERTED_TO_ORDER":
                offer.status = "EXPIRED"
                offer.cancelled_at = now
                messages = (
                    db.query(Message)
                    .filter(
                        Message.conversation_id == offer.conversation_id,
                        Message.message_type == "private_offer",
                    )
                    .all()
                )
                for message in messages:
                    try:
                        payload = json.loads(message.structured_payload_json or "{}")
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if payload.get("id") != offer.id:
                        continue
                    payload["status"] = "EXPIRED"
                    payload["cancelledAt"] = now.isoformat()
                    message.structured_payload_json = json.dumps(payload)
        order.status = "cancelled"
        order.updated_at = now
    db.commit()


def invalidate_other_private_offers(
    db: Session,
    *,
    listing_id: int,
    accepted_offer_id: str | None,
) -> None:
    """Invalidate remaining buyer-specific offers after inventory is paid."""
    offers = (
        db.query(PrivateOffer)
        .filter(
            PrivateOffer.product_id == listing_id,
            PrivateOffer.status.in_(("PENDING", "VIEWED")),
        )
        .all()
    )
    if not offers:
        return
    offer_ids = {offer.id for offer in offers if offer.id != accepted_offer_id}
    for offer in offers:
        if offer.id in offer_ids:
            offer.status = "INVALIDATED"
            offer.cancelled_at = datetime.now(timezone.utc)
    if not offer_ids:
        return
    conversation_ids = {offer.conversation_id for offer in offers if offer.id in offer_ids}
    messages = (
        db.query(Message)
        .filter(
            Message.conversation_id.in_(conversation_ids),
            Message.message_type == "private_offer",
        )
        .all()
    )
    for message in messages:
        try:
            payload = json.loads(message.structured_payload_json or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if payload.get("id") not in offer_ids:
            continue
        payload["status"] = "INVALIDATED"
        payload["cancelledAt"] = datetime.now(timezone.utc).isoformat()
        message.structured_payload_json = json.dumps(payload)


def listing_has_pending_pay(db: Session, listing_id: int) -> bool:
    return (
        db.query(Order.id)
        .filter(Order.listing_id == listing_id, Order.status == PENDING_PAY_STATUS)
        .first()
        is not None
    )


def compute_purchase_available(db: Session, listing: Listing) -> bool:
    checkout_amount = listing_checkout_amount(listing)
    return listing.status == "active" and checkout_amount > 0


def get_or_create_settings(db: Session, user_id: str):
    from app.models import UserSettings

    settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    if not settings:
        settings = UserSettings(user_id=user_id)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings
