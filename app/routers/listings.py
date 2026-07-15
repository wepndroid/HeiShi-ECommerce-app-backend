import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload

from app.auth import get_accept_language, get_current_user
from app.admin_notifications import notify_admin
from app.catalog_helpers import compute_purchase_available, reset_bundle_meta_for_resale
from app.config import settings
from app.database import get_db
from app.messaging_read import bump_unread_for_recipient
from app.models import Conversation, Favorite, Listing, Message, Order, User, ViewHistory
from app.moderation import find_blocked_keyword
from app.pagination import paginate
from app.platform_config import escrow_fee_from_db
from app.schemas import CreateListingRequest, ListingDetailDto, ListingSummaryDto, Paginated, UploadImageResponse, BundleItemRequest
from app.serializers import listing_to_detail, listing_to_summary
from app.storage import upload_image_bytes


def _build_bundle_meta(body: CreateListingRequest) -> dict:
    items = body.bundleItems or []
    share_total = round(sum(item.sharePrice for item in items), 2)
    if len(items) < 2:
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": "Bundle requires at least 2 items", "details": {}},
        )
    if abs(share_total - round(body.price, 2)) > 0.02:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "VALIDATION_ERROR",
                "message": "Bundle item shares must add up to the bundle price",
                "details": {"shareTotal": share_total, "price": body.price},
            },
        )
    for index, item in enumerate(items):
        image_urls = item.imageUrls or ([item.imageUrl] if item.imageUrl else [])
        if not image_urls:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "VALIDATION_ERROR",
                    "message": f"Bundle item {index + 1} requires at least one image",
                    "details": {},
                },
            )
        _validate_listing_image_urls(image_urls)
    return {
        "fullPrice": body.price,
        "pickupDeadline": body.pickupDeadline,
        "allowSeparateSale": body.allowSeparateSale if body.allowSeparateSale is not None else True,
        "pickupWindow": body.pickupWindow,
        "totalItems": len(items),
        "coverImageUrls": list(body.imageUrls or []),
        "items": [
            {
                "id": item.id or str(uuid.uuid4()),
                "title": item.title,
                "sharePrice": item.sharePrice,
                "separatePrice": item.separatePrice,
                "imageUrls": item.imageUrls or ([item.imageUrl] if item.imageUrl else []),
                "imageUrl": (item.imageUrls[0] if item.imageUrls else item.imageUrl),
                "status": "available",
            }
            for item in items
        ],
    }

router = APIRouter(tags=["listings"])
upload_router = APIRouter(prefix="/uploads", tags=["uploads"])

OPEN_ORDER_STATUSES = ("pendingPay", "pendingShip", "pendingReceive", "pendingReview")

ALLOWED_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
    "application/octet-stream",
    "binary/octet-stream",
}

_EXT_TO_TYPE = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _keyword_rejection_note(keyword: str) -> str:
    return f"Rejected by keyword pre-check: contains blocked keyword '{keyword}'."


def _apply_keyword_moderation(
    db: Session,
    listing: Listing,
    *,
    title: str,
    description: str | None,
    require_review_on_pass: bool,
) -> str | None:
    blocked = find_blocked_keyword(db, f"{title}\n{description or ''}")
    if blocked:
        listing.review_status = "rejected"
        listing.review_note = _keyword_rejection_note(blocked)
        return blocked
    if require_review_on_pass and listing.status != "draft":
        listing.review_status = "pendingReview"
        listing.review_note = None
    return None


def _valid_media_url(url: str) -> bool:
    trimmed = url.strip()
    if trimmed.startswith(("file://", "content://")):
        return False
    return trimmed.startswith(("http://", "https://", "/uploads/"))


def _validate_listing_image_urls(image_urls: list[str]) -> None:
    if not image_urls:
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": "At least one image required", "details": {}},
        )
    for url in image_urls:
        if not _valid_media_url(url):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "VALIDATION_ERROR",
                    "message": "Listing images must be uploaded http(s) or /uploads/ URLs",
                    "details": {},
                },
            )


def _resolve_upload_content_type(file: UploadFile, content: bytes) -> str | None:
    content_type = (file.content_type or "").lower()
    if content_type in {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}:
        return "image/jpeg" if content_type == "image/jpg" else content_type

    ext = Path(file.filename or "photo.jpg").suffix.lower()
    mapped = _EXT_TO_TYPE.get(ext)
    if mapped and content_type in {"application/octet-stream", "binary/octet-stream", "", "image/jpg"}:
        return mapped
    if mapped and content_type.startswith("image/"):
        return mapped

    if len(content) >= 3 and content[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(content) >= 6 and content[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def _ext_for_upload_type(resolved_type: str, filename: str | None) -> str:
    ext = Path(filename or "photo.jpg").suffix.lower()
    if ext in _EXT_TO_TYPE:
        return ext
    if resolved_type == "image/png":
        return ".png"
    if resolved_type == "image/webp":
        return ".webp"
    if resolved_type == "image/gif":
        return ".gif"
    return ".jpg"


@router.get("/listings/mine", response_model=Paginated[ListingSummaryDto])
def get_mine(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lang = get_accept_language(request)
    q = db.query(Listing).options(joinedload(Listing.seller)).filter(Listing.seller_id == user.id)
    if status == "active":
        q = q.filter(Listing.status == "active", Listing.review_status == "approved")
    elif status == "inactive":
        q = q.filter(
            or_(
                Listing.status == "inactive",
                and_(Listing.status == "active", Listing.review_status.in_(("pendingReview", "rejected", "removed"))),
            )
        )
    elif status == "draft":
        q = q.filter(Listing.status == "draft")
    elif status:
        q = q.filter(Listing.status == status)
    else:
        q = q.filter(Listing.status.in_(["active", "draft", "inactive"]))
    q = q.order_by(Listing.created_at.desc())
    total = q.count()
    items = q.offset((page - 1) * pageSize).limit(pageSize).all()
    return paginate([listing_to_summary(i, lang) for i in items], page, pageSize, total)


@router.get("/listings/sold", response_model=Paginated[ListingSummaryDto])
def get_sold(
    request: Request,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lang = get_accept_language(request)
    q = (
        db.query(Listing)
        .options(joinedload(Listing.seller))
        .filter(Listing.seller_id == user.id, Listing.status == "sold")
        .order_by(Listing.created_at.desc())
    )
    total = q.count()
    items = q.offset((page - 1) * pageSize).limit(pageSize).all()
    return paginate([listing_to_summary(i, lang) for i in items], page, pageSize, total)


@router.get("/listings/{listing_id}", response_model=ListingDetailDto)
def get_owned_listing(
    listing_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lang = get_accept_language(request)
    listing = (
        db.query(Listing)
        .options(joinedload(Listing.seller))
        .filter(Listing.id == listing_id, Listing.seller_id == user.id)
        .first()
    )
    if not listing:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Listing not found", "details": {}})
    detail = listing_to_detail(listing, lang, escrow_fee=escrow_fee_from_db(db))
    purchase_available = compute_purchase_available(db, listing)
    return detail.model_copy(update={"purchaseAvailable": purchase_available})


def _merge_bundle_meta_for_update(listing: Listing, body: CreateListingRequest) -> dict:
    existing = listing.bundle_meta if isinstance(listing.bundle_meta, dict) else {}
    existing_by_id = {
        row.get("id"): row
        for row in (existing.get("items") or [])
        if isinstance(row, dict) and row.get("id")
    }
    built = _build_bundle_meta(body)
    merged_items = []
    for item in built.get("items") or []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        prev = existing_by_id.get(row.get("id"))
        if prev and prev.get("status") in ("sold", "onHold"):
            row["status"] = prev["status"]
        merged_items.append(row)
    built["items"] = merged_items
    if body.imageUrls:
        built["coverImageUrls"] = list(body.imageUrls)
    return built


@router.post("/listings", response_model=ListingSummaryDto, status_code=201)
def create_listing(
    body: CreateListingRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    is_draft = body.status == "draft"
    # Admin publish-restriction (限制发布): blocks publishing new listings; drafts still allowed.
    if not is_draft and getattr(user, "publish_restricted", False):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "PUBLISH_RESTRICTED",
                "message": "Publishing is restricted on this account",
                "details": {},
            },
        )
    if not is_draft and not body.imageUrls:
        raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "At least one image required", "details": {}})
    if body.imageUrls:
        _validate_listing_image_urls(body.imageUrls)
    listing_type = body.type
    if listing_type == "job" and body.merchantPost and not user.business_verified:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "MERCHANT_VERIFICATION_REQUIRED",
                "message": "Merchant verification required for business gig posts",
                "details": {},
            },
        )
    if listing_type in ("service", "job") and not is_draft and not user.identity_verified:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "IDENTITY_REQUIRED",
                "message": "Identity verification required to publish this listing type",
                "details": {},
            },
        )
    review_status = "draft" if is_draft else "pendingReview"
    service_icon = None
    if listing_type == "bundle":
        if is_draft and (not body.bundleItems or len(body.bundleItems) < 2):
            bundle_meta = {
                "fullPrice": body.price,
                "pickupDeadline": body.pickupDeadline,
                "allowSeparateSale": body.allowSeparateSale if body.allowSeparateSale is not None else True,
                "pickupWindow": body.pickupWindow,
                "totalItems": len(body.bundleItems or []),
                "coverImageUrls": list(body.imageUrls or []),
                "items": [],
            }
        else:
            bundle_meta = _build_bundle_meta(body)
        tag_key = body.tagKey or "bundleSet"
        condition_key = body.conditionKey
        status = "draft" if is_draft else "active"
    else:
        bundle_meta = {}
        tag_key = body.tagKey or ""
        condition_key = body.conditionKey
        status = "draft" if is_draft else "active"
        if listing_type == "service":
            tag_key = body.tagKey or "localService"
            service_icon = body.serviceIcon or "truck"
            if service_icon not in ("truck", "broom", "cameraService"):
                service_icon = "truck"
    listing = Listing(
        seller_id=user.id,
        type=listing_type,
        title=body.title,
        description=body.description,
        price=body.price,
        category_key=body.categoryKey,
        tag_key=tag_key,
        condition_key=condition_key,
        location_label=body.locationLabel,
        region_state=body.regionState or "VIC",
        region_city=body.regionCity or "Melbourne",
        region_area=body.locationLabel,
        status=status,
        review_status=review_status,
        review_note=None,
    )
    listing.images = body.imageUrls
    listing.pickup_methods = body.pickupMethods or ["meetup"]
    listing.escrow_supported = True if body.escrowSupported is None else body.escrowSupported
    listing.meet_in_public = True if body.meetInPublic is None else body.meetInPublic
    listing.negotiable = bool(body.negotiable) if body.negotiable is not None else False
    listing.bundle_meta = bundle_meta
    if service_icon:
        listing.service_icon = service_icon
    if not is_draft:
        _apply_keyword_moderation(
            db,
            listing,
            title=body.title,
            description=body.description,
            require_review_on_pass=True,
        )
    db.add(listing)
    db.flush()
    if not is_draft and listing.review_status == "pendingReview":
        notify_admin(
            db,
            event_type="listing_pending_review",
            title="New listing requires review",
            body=f'{user.nickname} posted "{listing.title}".',
            target_type="listing",
            target_id=listing.id,
            action_path=f"/products/{listing.id}",
        )
    db.commit()
    db.refresh(listing)
    listing.seller = user
    return listing_to_summary(listing)


class UpdateListingRequest(BaseModel):
    type: str | None = None
    status: str | None = None
    title: str | None = None
    description: str | None = None
    price: float | None = None
    categoryKey: str | None = None
    conditionKey: str | None = None
    tagKey: str | None = None
    locationLabel: str | None = None
    imageUrls: list[str] | None = None
    pickupMethods: list[str] | None = None
    serviceIcon: str | None = None
    escrowSupported: bool | None = None
    negotiable: bool | None = None
    meetInPublic: bool | None = None
    bundleItems: list[BundleItemRequest] | None = None
    pickupDeadline: str | None = None
    allowSeparateSale: bool | None = None
    pickupWindow: str | None = None


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
    previous_price = listing.price
    material_keys = {"title", "description", "price", "imageUrls"} & data.keys()
    if material_keys:
        open_order = (
            db.query(Order)
            .filter(Order.listing_id == listing.id, Order.status.in_(OPEN_ORDER_STATUSES))
            .first()
        )
        if open_order:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "LISTING_HAS_ORDERS",
                    "message": "This listing has an active order and cannot be edited",
                    "details": {},
                },
            )
    field_map = {
        "categoryKey": "category_key",
        "conditionKey": "condition_key",
        "tagKey": "tag_key",
        "locationLabel": "location_label",
    }
    previous_status = listing.status
    previous_review_status = listing.review_status
    content_changed = False
    for key, val in data.items():
        if key == "type" and val is not None and val != listing.type:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_STATE", "message": "Cannot change listing type", "details": {}},
            )
        if key == "imageUrls" and val is not None:
            _validate_listing_image_urls(val)
            listing.images = val
        elif key == "pickupMethods" and val is not None:
            listing.pickup_methods = val
        elif key == "serviceIcon" and val is not None:
            listing.service_icon = val
        elif key == "escrowSupported" and val is not None:
            listing.escrow_supported = val
        elif key == "meetInPublic" and val is not None:
            listing.meet_in_public = val
        elif key == "negotiable" and val is not None:
            listing.negotiable = bool(val)
        elif key == "bundleItems" and val is not None:
            if listing.type != "bundle":
                raise HTTPException(
                    status_code=400,
                    detail={"code": "INVALID_STATE", "message": "Cannot update bundle items on non-bundle listing", "details": {}},
                )
            patch = CreateListingRequest(
                type="bundle",
                title=listing.title,
                description=listing.description,
                price=data.get("price", listing.price),
                categoryKey=listing.category_key,
                imageUrls=data.get("imageUrls", listing.images or []),
                locationLabel=listing.location_label,
                bundleItems=val,
                pickupDeadline=data.get("pickupDeadline"),
                allowSeparateSale=data.get("allowSeparateSale"),
                pickupWindow=data.get("pickupWindow"),
                pickupMethods=data.get("pickupMethods", listing.pickup_methods),
            )
            listing.bundle_meta = _merge_bundle_meta_for_update(listing, patch)
            if data.get("price") is not None:
                listing.price = data["price"]
        elif key == "pickupDeadline" and val is not None and listing.type == "bundle":
            meta = dict(listing.bundle_meta) if isinstance(listing.bundle_meta, dict) else {}
            meta["pickupDeadline"] = val
            listing.bundle_meta = meta
        elif key == "allowSeparateSale" and val is not None and listing.type == "bundle":
            meta = dict(listing.bundle_meta) if isinstance(listing.bundle_meta, dict) else {}
            meta["allowSeparateSale"] = val
            listing.bundle_meta = meta
        elif key == "pickupWindow" and val is not None and listing.type == "bundle":
            meta = dict(listing.bundle_meta) if isinstance(listing.bundle_meta, dict) else {}
            meta["pickupWindow"] = val
            listing.bundle_meta = meta
        elif key == "status" and val is not None:
            if val not in ("active", "draft", "inactive"):
                raise HTTPException(
                    status_code=400,
                    detail={"code": "VALIDATION_ERROR", "message": "Invalid listing status", "details": {}},
                )
            if listing.status == "sold":
                raise HTTPException(
                    status_code=400,
                    detail={"code": "INVALID_STATE", "message": "Cannot change sold listing status", "details": {}},
                )
            if val == "active" and listing.status in ("inactive", "draft"):
                listing.status = "active"
            elif val in ("inactive", "draft") and listing.status == "active":
                open_order = (
                    db.query(Order)
                    .filter(Order.listing_id == listing.id, Order.status.in_(OPEN_ORDER_STATUSES))
                    .first()
                )
                if open_order:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "LISTING_HAS_ORDERS",
                            "message": "This listing has an active order and cannot be deactivated",
                            "details": {},
                        },
                    )
                listing.status = val
        elif key in ("title", "description", "price"):
            setattr(listing, key, val)
            if key in ("title", "description"):
                content_changed = True
        elif key in field_map:
            setattr(listing, field_map[key], val)
            if key == "locationLabel" and val is not None:
                listing.region_area = val
    if listing.status != "draft" and (
        content_changed
        or (previous_status == "draft" and listing.status == "active")
        or (
            previous_status == "inactive"
            and listing.status == "active"
            and listing.review_status == "draft"
        )
        or previous_review_status in ("rejected", "removed")
    ):
        _apply_keyword_moderation(
            db,
            listing,
            title=listing.title,
            description=listing.description,
            require_review_on_pass=True,
        )
    if listing.review_status == "pendingReview" and previous_review_status != "pendingReview":
        notify_admin(
            db,
            event_type="listing_pending_review",
            title="Listing resubmitted for review",
            body=f'{user.nickname} resubmitted "{listing.title}".',
            target_type="listing",
            target_id=listing.id,
            action_path=f"/products/{listing.id}",
        )
    price_changed = "price" in data and abs(float(listing.price) - float(previous_price)) > 0.001
    if price_changed:
        now = datetime.now(timezone.utc)
        notice_text = f"__PRICE_CHANGE__:{float(listing.price):.2f}"
        conversations = db.query(Conversation).filter(Conversation.listing_id == listing.id).all()
        for conversation in conversations:
            message = Message(
                conversation_id=conversation.id,
                sender_id=user.id,
                text=notice_text,
                sent_at=now,
            )
            conversation.last_message_text = f"Price updated to A${float(listing.price):.2f}"
            conversation.last_message_at = now
            db.add(message)
            db.flush()
            bump_unread_for_recipient(db, conversation, user.id)
    db.commit()
    db.refresh(listing)
    return listing_to_summary(listing)


def _purge_listing(db: Session, listing: Listing) -> None:
    if listing.status == "sold":
        raise HTTPException(
            status_code=400,
            detail={"code": "LISTING_SOLD", "message": "Sold listings cannot be deleted", "details": {}},
        )
    if db.query(Order).filter(Order.listing_id == listing.id).first():
        raise HTTPException(
            status_code=409,
            detail={
                "code": "LISTING_HAS_ORDERS",
                "message": "This listing has orders and cannot be deleted",
                "details": {},
            },
        )
    if db.query(Conversation).filter(Conversation.listing_id == listing.id).first():
        raise HTTPException(
            status_code=409,
            detail={
                "code": "LISTING_HAS_CONVERSATIONS",
                "message": "This listing has messages and cannot be deleted",
                "details": {},
            },
        )
    db.query(Favorite).filter(Favorite.listing_id == listing.id).delete(synchronize_session=False)
    db.query(ViewHistory).filter(ViewHistory.listing_id == listing.id).delete(synchronize_session=False)
    db.delete(listing)


@router.delete("/listings/{listing_id}", status_code=204)
def delete_listing(listing_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    listing = db.query(Listing).filter(Listing.id == listing_id, Listing.seller_id == user.id).first()
    if not listing:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Listing not found", "details": {}})
    open_order = (
        db.query(Order)
        .filter(Order.listing_id == listing.id, Order.status.in_(OPEN_ORDER_STATUSES))
        .first()
    )
    if open_order:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "LISTING_HAS_ORDERS",
                "message": "This listing has an active order and cannot be removed",
                "details": {},
            },
        )
    if listing.status in ("draft", "inactive"):
        _purge_listing(db, listing)
    else:
        listing.status = "inactive"
    db.commit()
    return Response(status_code=204)


class ResaleRequest(BaseModel):
    title: str | None = None
    price: float | None = None


@router.post("/listings/resale/{source_listing_id}", response_model=ListingSummaryDto, status_code=201)
def create_resale(
    source_listing_id: int,
    request: Request,
    body: ResaleRequest | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lang = get_accept_language(request)
    source = db.query(Listing).options(joinedload(Listing.seller)).filter(Listing.id == source_listing_id).first()
    if not source:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Source listing not found", "details": {}})
    purchased = (
        db.query(Order)
        .filter(
            Order.listing_id == source_listing_id,
            Order.buyer_id == user.id,
            Order.status.in_(("completed", "pendingReview")),
        )
        .first()
    )
    if not purchased:
        raise HTTPException(
            status_code=403,
            detail={"code": "FORBIDDEN", "message": "Resale requires a confirmed purchase of this listing", "details": {}},
        )
    if source.status not in ("active", "sold"):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_STATE", "message": "Listing cannot be resold", "details": {}},
        )
    en_prefix = f"Resale: {source.title}"
    zh_prefix = f"转售: {source.title_zh or source.title}"
    existing_resale = (
        db.query(Listing)
        .filter(
            Listing.seller_id == user.id,
            Listing.status.in_(["active", "inactive", "draft"]),
            or_(Listing.title.like(f"{en_prefix}%"), Listing.title.like(f"{zh_prefix}%")),
        )
        .first()
    )
    if existing_resale:
        raise HTTPException(
            status_code=409,
            detail={"code": "RESALE_EXISTS", "message": "You already have a resale listing for this item", "details": {}},
        )
    if body and body.title and body.title.strip():
        resale_title = body.title.strip()
        resale_title_zh = f"转售: {resale_title}" if lang.startswith("zh") else (f"转售: {source.title_zh}" if source.title_zh else None)
    elif lang.startswith("zh"):
        resale_title = zh_prefix
        resale_title_zh = zh_prefix
    else:
        resale_title = en_prefix
        resale_title_zh = zh_prefix if source.title_zh else None
    resale_price = body.price if body and body.price is not None and body.price > 0 else source.price
    listing = Listing(
        seller_id=user.id,
        type=source.type,
        title=resale_title,
        title_zh=resale_title_zh,
        description=source.description,
        description_zh=source.description_zh,
        price=resale_price,
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
    listing.bundle_meta = reset_bundle_meta_for_resale(source.bundle_meta)
    if source.service_icon:
        listing.service_icon = source.service_icon
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
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "File too large (max 10MB)", "details": {}})
    resolved_type = _resolve_upload_content_type(file, content)
    if not resolved_type:
        raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "Invalid file type", "details": {}})
    ext = _ext_for_upload_type(resolved_type, file.filename)
    url, key = upload_image_bytes(content, resolved_type, ext, user_id=user.id)
    return UploadImageResponse(url=url, key=key)
