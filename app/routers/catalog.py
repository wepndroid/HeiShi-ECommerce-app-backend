from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session, joinedload

from app.auth import get_accept_language
from app.catalog_helpers import apply_region_filter, apply_search, apply_tab_filter
from app.database import get_db
from app.models import Listing
from app.pagination import paginate
from app.schemas import ListingDetailDto, ListingSummaryDto, LocalServiceDto, Paginated, SuggestionDto
from app.serializers import listing_to_detail, listing_to_service, listing_to_summary

router = APIRouter(prefix="/catalog", tags=["catalog"])


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
):
    lang = get_accept_language(request)
    q = db.query(Listing).options(joinedload(Listing.seller)).filter(Listing.status == "active")
    q = apply_region_filter(q, regionState, regionCity, regionArea)
    q = apply_tab_filter(q, tab)
    if categoryKey:
        q = q.filter(Listing.category_key == categoryKey)
    q = q.order_by(Listing.created_at.desc())
    total = q.count()
    items = q.offset((page - 1) * pageSize).limit(pageSize).all()
    return paginate([listing_to_summary(i, lang) for i in items], page, pageSize, total)


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
):
    lang = get_accept_language(request)
    query = db.query(Listing).options(joinedload(Listing.seller)).filter(Listing.status == "active")
    query = apply_region_filter(query, regionState, regionCity, regionArea)
    query = apply_tab_filter(query, tab)
    if categoryKey:
        query = query.filter(Listing.category_key == categoryKey)
    query = apply_search(query, q, sort)
    total = query.count()
    items = query.offset((page - 1) * pageSize).limit(pageSize).all()
    return paginate([listing_to_summary(i, lang) for i in items], page, pageSize, total)


@router.get("/listings/{listing_id}", response_model=ListingDetailDto)
def get_listing(listing_id: int, request: Request, db: Session = Depends(get_db)):
    lang = get_accept_language(request)
    listing = (
        db.query(Listing).options(joinedload(Listing.seller)).filter(Listing.id == listing_id).first()
    )
    if not listing or listing.status == "inactive":
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Listing not found", "details": {}})
    listing.view_count += 1
    db.commit()
    db.refresh(listing)
    return listing_to_detail(listing, lang)


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
):
    lang = get_accept_language(request)
    source = db.query(Listing).filter(Listing.id == listing_id).first()
    if not source:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Listing not found", "details": {}})
    q = (
        db.query(Listing)
        .options(joinedload(Listing.seller))
        .filter(Listing.status == "active", Listing.id != listing_id, Listing.category_key == source.category_key)
    )
    q = apply_region_filter(q, regionState, regionCity, regionArea)
    q = q.order_by(Listing.created_at.desc())
    total = q.count()
    items = q.offset((page - 1) * pageSize).limit(pageSize).all()
    return paginate([listing_to_summary(i, lang) for i in items], page, pageSize, total)


@router.get("/services", response_model=Paginated[LocalServiceDto])
def get_services(
    request: Request,
    regionState: str | None = None,
    regionCity: str | None = None,
    regionArea: str | None = None,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    lang = get_accept_language(request)
    q = (
        db.query(Listing)
        .options(joinedload(Listing.seller))
        .filter(Listing.status == "active", Listing.type == "service")
    )
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
    db: Session = Depends(get_db),
):
    lang = get_accept_language(request)
    q = db.query(Listing).filter(Listing.status == "active").order_by(Listing.view_count.desc()).limit(8)
    q = apply_region_filter(q, regionState, regionCity, regionArea)
    results = []
    for listing in q.all():
        title = listing.title_zh if lang == "zh" and listing.title_zh else listing.title
        words = title.split()
        query = words[0].lower() if words else title[:10].lower()
        results.append(
            SuggestionDto(
                query=query,
                listingId=listing.id,
                title=title,
                subtitle=f"A${listing.price:.0f} · {listing.location_label}",
            )
        )
    return results
