from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from PIL import UnidentifiedImageError
from sqlalchemy import case, func
from sqlalchemy.orm import Session, joinedload

from app.analytics import record_promotion_click
from app.auth import get_accept_language, get_current_user, get_current_user_optional
from app.blocklist_helpers import exclude_blocked_sellers, users_blocked
from app.catalog_helpers import (
    apply_feed_listing_status_filter,
    apply_feed_sort,
    apply_region_filter,
    apply_search,
    apply_tab_filter,
    compute_purchase_available,
    exclude_unpaid_reserved,
    expire_stale_pending_pay_orders,
)
from app.config import settings
from app.database import get_db
from app.form_options import LISTING_FORM_OPTIONS
from app.image_search import hamming_distance, hash_image_bytes, hash_image_url, is_similar_enough
from app.media_urls import normalize_media_url, normalize_media_urls
from app.models import Favorite, Listing, Order, Review, User, ViewHistory
from app.pagination import paginate
from app.schemas import (
    ImageSearchResponseDto,
    ListingDetailDto,
    ListingFormOptionsDto,
    ListingSummaryDto,
    LocalServiceDto,
    Paginated,
    SuggestionDto,
)
from app.serializers import listing_to_detail, listing_to_service, listing_to_summary

router = APIRouter(prefix="/catalog", tags=["catalog"])


def _seller_completed_counts(db: Session, listings: list[Listing]) -> dict[str, int]:
    seller_ids = {item.seller_id for item in listings if item.seller_id}
    if not seller_ids:
        return {}
    rows = (
        db.query(Order.seller_id, func.count(Order.id))
        .filter(
            Order.seller_id.in_(seller_ids),
            Order.status.in_(("completed", "pendingReview")),
        )
        .group_by(Order.seller_id)
        .all()
    )
    return dict(rows)


def _seller_positive_rating_rates(db: Session, listings: list[Listing]) -> dict[str, int]:
    seller_ids = {item.seller_id for item in listings if item.seller_id}
    if not seller_ids:
        return {}
    rows = (
        db.query(
            Order.seller_id,
            func.count(Review.id),
            func.sum(case((Review.rating >= 4, 1), else_=0)),
        )
        .join(Review, Review.order_id == Order.id)
        .filter(Order.seller_id.in_(seller_ids))
        .group_by(Order.seller_id)
        .all()
    )
    rates: dict[str, int] = {}
    for seller_id, total, positive in rows:
        if total and positive is not None:
            rates[seller_id] = round(int(positive) * 100 / int(total))
    return rates


def _summarize_listings(db: Session, items: list[Listing], lang: str) -> list[ListingSummaryDto]:
    counts = _seller_completed_counts(db, items)
    rates = _seller_positive_rating_rates(db, items)
    return [
        listing_to_summary(
            item,
            lang,
            completed_order_count=counts.get(item.seller_id, 0),
            positive_rating_rate=rates.get(item.seller_id),
        )
        for item in items
    ]


def _user_can_view_inactive_listing(db: Session, listing: Listing, user: User | None) -> bool:
    if listing.status == "active":
        return True
    if user is None:
        return False
    if listing.seller_id == user.id:
        return True
    has_order = (
        db.query(Order.id)
        .filter(
            Order.listing_id == listing.id,
            Order.status != "cancelled",
            (Order.buyer_id == user.id) | (Order.seller_id == user.id),
        )
        .first()
        is not None
    )
    if has_order:
        return True
    favorited = (
        db.query(Favorite.id)
        .filter(Favorite.user_id == user.id, Favorite.listing_id == listing.id)
        .first()
        is not None
    )
    if favorited:
        return True
    viewed = (
        db.query(ViewHistory.id)
        .filter(ViewHistory.user_id == user.id, ViewHistory.listing_id == listing.id)
        .first()
        is not None
    )
    return viewed


@router.get("/form-options", response_model=ListingFormOptionsDto)
def get_form_options(db: Session = Depends(get_db)):
    from app.platform_config import form_options_from_db

    return form_options_from_db(db)


@router.get("/banners")
def get_banners(position: str = Query("home"), db: Session = Depends(get_db)):
    from app.platform_config import banners_from_db

    return {"items": banners_from_db(db, position)}


@router.get("/feed", response_model=Paginated[ListingSummaryDto])
def get_feed(
    request: Request,
    regionState: str | None = None,
    regionCity: str | None = None,
    regionArea: str | None = None,
    tab: str | None = None,
    categoryKey: str | None = None,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    lang = get_accept_language(request)
    expire_stale_pending_pay_orders(db, settings.pending_pay_expire_minutes)
    viewer_id = user.id if user else None
    q = db.query(Listing).options(joinedload(Listing.seller))
    q = apply_feed_listing_status_filter(q, db, viewer_id)
    q = exclude_unpaid_reserved(q, db, viewer_id)
    q = exclude_blocked_sellers(q, db, user.id if user else None)
    q = apply_region_filter(q, regionState, regionCity, regionArea)
    q = apply_tab_filter(q, tab)
    if categoryKey and tab != "services":
        q = q.filter(Listing.category_key == categoryKey)
    q = apply_feed_sort(q)
    total = q.count()
    items = q.offset((page - 1) * pageSize).limit(pageSize).all()
    return paginate(_summarize_listings(db, items, lang), page, pageSize, total)


@router.post("/search/image", response_model=ImageSearchResponseDto)
async def search_by_image(
    request: Request,
    file: UploadFile = File(...),
    regionState: str | None = None,
    regionCity: str | None = None,
    regionArea: str | None = None,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    lang = get_accept_language(request)
    data = await file.read()
    if not data:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_IMAGE", "message": "Image file is empty", "details": {}},
        )

    try:
        query_hash = hash_image_bytes(data)
    except UnidentifiedImageError:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_IMAGE", "message": "Unsupported image format", "details": {}},
        )

    q = db.query(Listing).options(joinedload(Listing.seller))
    viewer_id = user.id if user else None
    q = apply_feed_listing_status_filter(q, db, viewer_id)
    q = exclude_unpaid_reserved(q, db, viewer_id)
    q = exclude_blocked_sellers(q, db, user.id if user else None)
    q = apply_region_filter(q, regionState, regionCity, regionArea)
    listings = q.all()

    scored: list[tuple[int, Listing]] = []
    for listing in listings:
        urls = normalize_media_urls(list(listing.images or []))
        if not urls:
            cover = normalize_media_url(listing.image_url)
            if cover:
                urls = [cover]
        best_distance: int | None = None
        for img_url in urls:
            listing_hash = hash_image_url(img_url)
            if not listing_hash:
                continue
            distance = hamming_distance(query_hash, listing_hash)
            if is_similar_enough(distance) and (best_distance is None or distance < best_distance):
                best_distance = distance
        if best_distance is not None:
            scored.append((best_distance, listing))

    scored.sort(key=lambda item: item[0])
    total = len(scored)
    offset = (page - 1) * pageSize
    page_items = scored[offset : offset + pageSize]
    summaries = _summarize_listings(db, [listing for _, listing in page_items], lang)

    top = scored[0][1] if scored else None
    if top:
        suggested = top.title_zh if lang == "zh" and top.title_zh else top.title
    else:
        stem = Path(file.filename or "photo").stem.strip()
        suggested = stem if stem and stem.lower() not in {"photo", "image", "img"} else ""

    page_result = paginate(summaries, page, pageSize, total)
    return ImageSearchResponseDto(
        suggestedQuery=suggested,
        matchCount=total,
        items=page_result.items,
        page=page_result.page,
        pageSize=page_result.pageSize,
        total=page_result.total,
        hasMore=page_result.hasMore,
    )


@router.get("/search", response_model=Paginated[ListingSummaryDto])
def search(
    request: Request,
    regionState: str | None = None,
    regionCity: str | None = None,
    regionArea: str | None = None,
    tab: str | None = None,
    categoryKey: str | None = None,
    q: str | None = None,
    sort: str | None = None,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    lang = get_accept_language(request)
    viewer_id = user.id if user else None
    query = db.query(Listing).options(joinedload(Listing.seller))
    query = apply_feed_listing_status_filter(query, db, viewer_id)
    query = exclude_unpaid_reserved(query, db, viewer_id)
    query = exclude_blocked_sellers(query, db, user.id if user else None)
    query = apply_region_filter(query, regionState, regionCity, regionArea)
    query = apply_tab_filter(query, tab)
    if categoryKey:
        query = query.filter(Listing.category_key == categoryKey)
    query = apply_search(query, q, sort)
    total = query.count()
    items = query.offset((page - 1) * pageSize).limit(pageSize).all()
    return paginate(_summarize_listings(db, items, lang), page, pageSize, total)


@router.get("/listings/{listing_id}", response_model=ListingDetailDto)
def get_listing(
    listing_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    lang = get_accept_language(request)
    listing = (
        db.query(Listing).options(joinedload(Listing.seller)).filter(Listing.id == listing_id).first()
    )
    if not listing or not _user_can_view_inactive_listing(db, listing, user):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Listing not found", "details": {}})
    if user and listing.seller_id != user.id and users_blocked(db, user.id, listing.seller_id):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Listing not found", "details": {}})
    expire_stale_pending_pay_orders(db, settings.pending_pay_expire_minutes)
    detail = listing_to_detail(listing, lang)
    purchase_available = compute_purchase_available(db, listing)
    return detail.model_copy(update={"purchaseAvailable": purchase_available})


@router.get("/listings/{listing_id}/related", response_model=Paginated[ListingSummaryDto])
def get_related(
    listing_id: int,
    request: Request,
    regionState: str | None = None,
    regionCity: str | None = None,
    regionArea: str | None = None,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    lang = get_accept_language(request)
    source = db.query(Listing).filter(Listing.id == listing_id).first()
    if not source:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Listing not found", "details": {}})
    q = (
        db.query(Listing)
        .options(joinedload(Listing.seller))
        .filter(Listing.id != listing_id, Listing.category_key == source.category_key)
    )
    viewer_id = user.id if user else None
    q = apply_feed_listing_status_filter(q, db, viewer_id)
    q = exclude_unpaid_reserved(q, db, viewer_id)
    q = exclude_blocked_sellers(q, db, user.id if user else None)
    q = apply_region_filter(q, regionState, regionCity, regionArea)
    q = q.order_by(Listing.created_at.desc())
    total = q.count()
    items = q.offset((page - 1) * pageSize).limit(pageSize).all()
    return paginate(_summarize_listings(db, items, lang), page, pageSize, total)


@router.get("/services", response_model=Paginated[LocalServiceDto])
def get_services(
    request: Request,
    regionState: str | None = None,
    regionCity: str | None = None,
    regionArea: str | None = None,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    lang = get_accept_language(request)
    viewer_id = user.id if user else None
    q = db.query(Listing).options(joinedload(Listing.seller)).filter(Listing.type == "service")
    q = apply_feed_listing_status_filter(q, db, viewer_id)
    q = exclude_unpaid_reserved(q, db, viewer_id)
    q = apply_region_filter(q, regionState, regionCity, regionArea)
    total = q.count()
    items = q.offset((page - 1) * pageSize).limit(pageSize).all()
    return paginate([listing_to_service(i, lang) for i in items], page, pageSize, total)


@router.get("/suggestions", response_model=list[SuggestionDto])
def get_suggestions(
    request: Request,
    regionState: str | None = None,
    regionCity: str | None = None,
    regionArea: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    lang = get_accept_language(request)
    viewer_id = user.id if user else None
    qry = db.query(Listing)
    qry = apply_feed_listing_status_filter(qry, db, viewer_id)
    qry = exclude_unpaid_reserved(qry, db, viewer_id)
    qry = apply_region_filter(qry, regionState, regionCity, regionArea)
    if q and q.strip():
        qry = apply_search(qry, q.strip(), "relevance")
    else:
        qry = qry.order_by(Listing.view_count.desc())
    qry = qry.limit(4)
    results = []
    for listing in qry.all():
        title = listing.title_zh if lang == "zh" and listing.title_zh else listing.title
        words = title.split()
        query = words[0].lower() if words else title[:10].lower()
        cover = normalize_media_url(listing.image_url)
        if not cover and listing.images:
            cover = normalize_media_url(listing.images[0])
        results.append(
            SuggestionDto(
                query=query,
                listingId=listing.id,
                title=title,
                subtitle=f"A${listing.price:.0f} · {listing.location_label}",
                imageUrl=cover,
            )
        )
    return results


@router.post("/listings/{listing_id}/promotion-click", status_code=204)
def promotion_click(
    listing_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    record_promotion_click(db, listing_id, user.id if user else None)
    return Response(status_code=204)
