from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Query, Session

from app.models import Conversation, ExposureRule, Listing, Order, Review

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


def apply_feed_sort(q: Query) -> Query:
    """Apply active admin exposure rules, then the normal organic ranking.

    A correlated subquery avoids duplicate listings when more than one rule targets
    the same product. Suppression is server-enforced and expired rules have no effect.
    """
    now = datetime.now(timezone.utc)
    target_matches = and_(
        or_(
            ExposureRule.target_region.is_(None),
            ExposureRule.target_region == Listing.region_state,
            ExposureRule.target_region == Listing.region_city,
            ExposureRule.target_region == Listing.region_area,
        ),
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
    suppressed = exists(
        select(ExposureRule.id).where(
            active_rule,
            ExposureRule.rule_type.in_(("suppress", "exclude")),
        )
    )
    pinned = exists(
        select(ExposureRule.id).where(active_rule, ExposureRule.rule_type == "pin")
    )
    exposure_weight = (
        select(func.max(ExposureRule.exposure_weight))
        .where(active_rule, ExposureRule.rule_type != "suppress")
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
    seller_rating = (
        select(func.avg(Review.rating))
        .join(Order, Review.order_id == Order.id)
        .where(Order.seller_id == Listing.seller_id, Review.is_hidden.is_(False), Review.is_removed.is_(False))
        .correlate(Listing)
        .scalar_subquery()
    )
    organic_score = (
        func.coalesce(Listing.favorite_count, 0) * 0.25
        + func.coalesce(Listing.view_count, 0) * 0.01
        + func.coalesce(conversation_count, 0) * 0.2
        + func.coalesce(completed_order_count, 0) * 1.0
        + func.coalesce(seller_rating, 0) * 0.1
    )
    q = q.filter(~suppressed)
    return q.order_by(
        pinned.desc(),
        func.coalesce(exposure_weight, 1.0).desc(),
        Listing.is_pinned.desc(),
        Listing.is_recommended.desc(),
        organic_score.desc(),
        Listing.created_at.desc(),
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
    return q.filter(Listing.status == "active", Listing.review_status == "approved")


def apply_public_listing_visibility_filter(q: Query) -> Query:
    """Public seller/profile pages and direct public detail views."""
    return q.filter(Listing.status == "active", Listing.review_status == "approved")


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
        order.status = "cancelled"
        order.updated_at = now
    db.commit()


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
