import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import Listing, User
from app.pagination import paginate
from app.schemas import CreateListingRequest, ListingSummaryDto, Paginated, UploadImageResponse
from app.serializers import listing_to_summary

router = APIRouter(tags=["listings"])
upload_router = APIRouter(prefix="/uploads", tags=["uploads"])

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


@router.get("/listings/mine", response_model=Paginated[ListingSummaryDto])
def get_mine(
    status: str | None = None,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(Listing).options(joinedload(Listing.seller)).filter(Listing.seller_id == user.id)
    if status:
        q = q.filter(Listing.status == status)
    else:
        q = q.filter(Listing.status.in_(["active", "draft", "inactive"]))
    q = q.order_by(Listing.created_at.desc())
    total = q.count()
    items = q.offset((page - 1) * pageSize).limit(pageSize).all()
    return paginate([listing_to_summary(i) for i in items], page, pageSize, total)


@router.get("/listings/sold", response_model=Paginated[ListingSummaryDto])
def get_sold(
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = (
        db.query(Listing)
        .options(joinedload(Listing.seller))
        .filter(Listing.seller_id == user.id, Listing.status == "sold")
        .order_by(Listing.created_at.desc())
    )
    total = q.count()
    items = q.offset((page - 1) * pageSize).limit(pageSize).all()
    return paginate([listing_to_summary(i) for i in items], page, pageSize, total)


@router.post("/listings", response_model=ListingSummaryDto, status_code=201)
def create_listing(
    body: CreateListingRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not body.imageUrls:
        raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "At least one image required", "details": {}})
    listing = Listing(
        seller_id=user.id,
        type=body.type,
        title=body.title,
        description=body.description,
        price=body.price,
        category_key=body.categoryKey,
        tag_key=body.tagKey or "",
        condition_key=body.conditionKey,
        location_label=body.locationLabel,
        region_state="VIC",
        region_city="Melbourne",
        region_area=body.locationLabel,
        status="active",
    )
    listing.images = body.imageUrls
    listing.pickup_methods = body.pickupMethods or ["meetup"]
    db.add(listing)
    db.commit()
    db.refresh(listing)
    listing.seller = user
    return listing_to_summary(listing)


class UpdateListingRequest(BaseModel):
    type: str | None = None
    title: str | None = None
    description: str | None = None
    price: float | None = None
    categoryKey: str | None = None
    conditionKey: str | None = None
    tagKey: str | None = None
    locationLabel: str | None = None
    imageUrls: list[str] | None = None
    pickupMethods: list[str] | None = None


@router.patch("/listings/{listing_id}", response_model=ListingSummaryDto)
def update_listing(
    listing_id: int,
    body: UpdateListingRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    listing = db.query(Listing).options(joinedload(Listing.seller)).filter(Listing.id == listing_id).first()
    if not listing or listing.seller_id != user.id:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Listing not found", "details": {}})
    if listing.status == "sold":
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATE", "message": "Cannot edit sold listing", "details": {}})
    data = body.model_dump(exclude_unset=True)
    field_map = {
        "categoryKey": "category_key",
        "conditionKey": "condition_key",
        "tagKey": "tag_key",
        "locationLabel": "location_label",
    }
    for key, val in data.items():
        if key == "imageUrls" and val is not None:
            listing.images = val
        elif key == "pickupMethods" and val is not None:
            listing.pickup_methods = val
        elif key in ("title", "description", "price", "type"):
            setattr(listing, key, val)
        elif key in field_map:
            setattr(listing, field_map[key], val)
    db.commit()
    db.refresh(listing)
    return listing_to_summary(listing)


@router.delete("/listings/{listing_id}", status_code=204)
def delete_listing(listing_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    listing = db.query(Listing).filter(Listing.id == listing_id, Listing.seller_id == user.id).first()
    if not listing:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Listing not found", "details": {}})
    listing.status = "inactive"
    db.commit()
    return Response(status_code=204)


@router.post("/listings/resale/{source_listing_id}", response_model=ListingSummaryDto, status_code=201)
def create_resale(
    source_listing_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    source = db.query(Listing).options(joinedload(Listing.seller)).filter(Listing.id == source_listing_id).first()
    if not source:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Source listing not found", "details": {}})
    listing = Listing(
        seller_id=user.id,
        type=source.type,
        title=f"Resale: {source.title}",
        title_zh=f"转售: {source.title_zh}" if source.title_zh else None,
        description=source.description,
        description_zh=source.description_zh,
        price=source.price,
        category_key=source.category_key,
        tag_key=source.tag_key,
        condition_key=source.condition_key,
        location_label=source.location_label,
        region_state=source.region_state,
        region_city=source.region_city,
        region_area=source.region_area,
        status="draft",
    )
    listing.images = source.images
    listing.pickup_methods = source.pickup_methods
    db.add(listing)
    db.commit()
    db.refresh(listing)
    listing.seller = user
    return listing_to_summary(listing)


@upload_router.post("/images", response_model=UploadImageResponse)
async def upload_image(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "Invalid file type", "details": {}})
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "File too large (max 10MB)", "details": {}})
    ext = Path(file.filename or "photo.jpg").suffix or ".jpg"
    key = f"{uuid.uuid4().hex}{ext}"
    upload_path = Path(settings.upload_dir)
    upload_path.mkdir(parents=True, exist_ok=True)
    dest = upload_path / key
    with open(dest, "wb") as f:
        f.write(content)
    url = f"{settings.base_url}/uploads/{key}"
    return UploadImageResponse(url=url, key=key)
