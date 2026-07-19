"""Cross-client platform modules introduced by the expanded backend specification.

The routes in this module deliberately keep authorization and state transitions on
the server. Mobile and admin clients may render the state, but cannot manufacture
offers, orders, share attribution, or mandatory-notification preferences.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.admin_auth import require_admin
from app.auth import get_current_user, get_current_user_optional
from app.database import get_db
from app.models import (
    AdminConversation,
    AdminSupportMessage,
    AnonymousSession,
    Conversation,
    ExposureRule,
    Listing,
    MediaAsset,
    Message,
    NotificationPreference,
    Order,
    PrivateOffer,
    ShareAttributionEvent,
    ShareRecord,
    UploadSession,
    User,
    ensure_utc,
    utcnow,
)
from app.notification_jobs import enqueue_notification
from app.config import settings
from app.media_processing import MediaValidationError, process_image_variants
from app.storage import (
    create_signed_upload,
    storage_backend,
    supabase_public_url,
    upload_image_bytes,
)
from app.video_processing import (
    VideoProcessingError,
    process_video_variants,
    video_processor_available,
)

router = APIRouter(tags=["platform-features"])
admin_router = APIRouter(prefix="/admin", tags=["admin-platform-features"])

OFFER_ACTIVE_STATES = {"PENDING", "VIEWED"}
OFFER_TERMINAL_STATES = {"REJECTED", "CANCELLED", "EXPIRED", "CONVERTED_TO_ORDER", "INVALIDATED"}
MANDATORY_NOTIFICATION_CATEGORIES = {
    "payment_update",
    "refund_update",
    "payout",
    "dispute",
    "account_security",
    "moderation",
}
DEFAULT_NOTIFICATION_CATEGORIES = {
    "buyer": (
        "marketing",
        "product_recommendation",
        "chat_message",
        "order_update",
        "payment_update",
        "delivery_update",
        "refund_update",
        "dispute",
        "account_security",
        "platform_notice",
    ),
    "seller": (
        "marketing",
        "product_recommendation",
        "chat_message",
        "order_update",
        "payment_update",
        "delivery_update",
        "refund_update",
        "payout",
        "dispute",
        "moderation",
        "account_security",
        "platform_notice",
    ),
}
ALLOWED_MEDIA_TYPES = {"image", "video"}
ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_VIDEO_CONTENT_TYPES = {"video/mp4", "video/quicktime", "video/webm"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 500 * 1024 * 1024


def _not_found(resource: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "NOT_FOUND", "message": f"{resource} not found", "details": {}},
    )


def _forbidden(message: str) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={"code": "FORBIDDEN", "message": message, "details": {}},
    )


def _conflict(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": code, "message": message, "details": {}},
    )


def _offer_payload(row: PrivateOffer) -> dict:
    return {
        "id": row.id,
        "productId": row.product_id,
        "sellerId": row.seller_id,
        "buyerId": row.buyer_id,
        "conversationId": row.conversation_id,
        "originalPrice": row.original_price,
        "negotiatedPrice": row.negotiated_price,
        "currency": row.currency,
        "quantity": row.quantity,
        "shippingFee": row.shipping_fee,
        "totalAmount": row.total_amount,
        "expirationTime": ensure_utc(row.expiration_time).isoformat(),
        "status": row.status,
        "orderId": row.order_id,
        "acceptedAt": ensure_utc(row.accepted_at).isoformat() if row.accepted_at else None,
        "cancelledAt": ensure_utc(row.cancelled_at).isoformat() if row.cancelled_at else None,
        "createdAt": ensure_utc(row.created_at).isoformat(),
    }


def _refresh_offer_message(db: Session, offer: PrivateOffer) -> None:
    """Keep the structured chat card synchronized with the server offer state."""
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
        except json.JSONDecodeError:
            continue
        if payload.get("id") == offer.id:
            message.structured_payload_json = json.dumps(_offer_payload(offer))


def _asset_payload(
    row: MediaAsset,
    session: UploadSession | None = None,
    *,
    direct_upload: dict[str, object] | None = None,
) -> dict:
    payload = {
        "id": row.id,
        "mediaType": row.media_type,
        "status": row.status,
        "contentType": row.content_type,
        "fileSize": row.file_size,
        "checksumSha256": row.checksum_sha256,
        "originalUrl": row.original_url,
        "thumbnailUrl": row.thumbnail_url,
        "variants": json.loads(row.variants_json or "{}"),
        "width": row.width,
        "height": row.height,
        "durationSeconds": row.duration_seconds,
        "processingError": row.processing_error,
        "retryCount": row.retry_count,
    }
    if session:
        payload["uploadSession"] = {
            "id": session.id,
            "status": session.status,
            "expiresAt": ensure_utc(session.expires_at).isoformat(),
            "completeUrl": f"/v1/media/upload-sessions/{session.id}/complete",
            "chunkUrl": f"/v1/media/upload-sessions/{session.id}/chunk",
            "finalizeUrl": f"/v1/media/upload-sessions/{session.id}/finalize",
            "bytesUploaded": session.bytes_uploaded,
            "totalBytes": session.total_bytes,
        }
    if direct_upload:
        payload["directUpload"] = direct_upload
    return payload


class CreateUploadSessionRequest(BaseModel):
    media_type: str = Field(alias="mediaType")
    content_type: str = Field(alias="contentType")
    filename: str = Field(min_length=1, max_length=255)
    file_size: int = Field(alias="fileSize", gt=0)
    checksum_sha256: str | None = Field(default=None, alias="checksumSha256")
    listing_id: int | None = Field(default=None, alias="listingId")


class CompleteUploadRequest(BaseModel):
    original_url: str = Field(alias="originalUrl", min_length=1, max_length=1000)
    thumbnail_url: str | None = Field(default=None, alias="thumbnailUrl", max_length=1000)
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    duration_seconds: float | None = Field(default=None, alias="durationSeconds", ge=0)


@router.post("/media/upload-sessions", status_code=201)
def create_upload_session(
    body: CreateUploadSessionRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    media_type = body.media_type.lower().strip()
    content_type = body.content_type.lower().strip()
    if media_type not in ALLOWED_MEDIA_TYPES:
        raise HTTPException(status_code=422, detail="Unsupported media type")
    allowed = ALLOWED_IMAGE_CONTENT_TYPES if media_type == "image" else ALLOWED_VIDEO_CONTENT_TYPES
    size_limit = MAX_IMAGE_BYTES if media_type == "image" else MAX_VIDEO_BYTES
    if content_type not in allowed:
        raise HTTPException(status_code=422, detail="Unsupported content type")
    if body.file_size > size_limit:
        raise HTTPException(status_code=413, detail="Media file is too large")
    checksum = body.checksum_sha256.lower() if body.checksum_sha256 else None
    if checksum and (len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum)):
        raise HTTPException(status_code=422, detail="checksumSha256 must be a SHA-256 hex digest")
    if body.listing_id is not None:
        listing = db.query(Listing).filter(Listing.id == body.listing_id).first()
        if not listing:
            raise _not_found("Listing")
        if listing.seller_id != user.id:
            raise _forbidden("Only the listing owner can attach media")
    if checksum:
        existing = (
            db.query(MediaAsset)
            .filter(
                MediaAsset.owner_id == user.id,
                MediaAsset.checksum_sha256 == checksum,
                MediaAsset.status == "READY",
            )
            .first()
        )
        if existing:
            return {**_asset_payload(existing), "deduplicated": True}
    suffix = body.filename.rsplit(".", 1)[-1].lower() if "." in body.filename else "bin"
    storage_key = f"{user.id}/{media_type}/{secrets.token_urlsafe(18)}.{suffix}"
    asset = MediaAsset(
        owner_id=user.id,
        listing_id=body.listing_id,
        media_type=media_type,
        status="PENDING_UPLOAD",
        original_filename=body.filename,
        content_type=content_type,
        file_size=body.file_size,
        checksum_sha256=checksum,
        storage_key=storage_key,
    )
    db.add(asset)
    db.flush()
    session = UploadSession(
        media_asset_id=asset.id,
        owner_id=user.id,
        total_bytes=body.file_size,
        expires_at=utcnow() + timedelta(hours=1),
    )
    db.add(session)
    db.commit()
    db.refresh(asset)
    db.refresh(session)
    direct_upload = create_signed_upload(asset.storage_key, asset.content_type)
    return {
        **_asset_payload(asset, session, direct_upload=direct_upload),
        "deduplicated": False,
    }


def _upload_part_path(session_id: str) -> Path:
    root = Path(settings.upload_dir) / ".upload-sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{session_id}.part"


@router.put("/media/upload-sessions/{session_id}/chunk")
async def upload_media_chunk(
    session_id: str,
    request: Request,
    offset: int = Query(ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    upload = (
        db.query(UploadSession)
        .filter(UploadSession.id == session_id)
        .with_for_update()
        .first()
    )
    if not upload:
        raise _not_found("Upload session")
    if upload.owner_id != user.id:
        raise _forbidden("You cannot upload to this session")
    if ensure_utc(upload.expires_at) <= utcnow():
        upload.status = "EXPIRED"
        db.commit()
        raise _conflict("UPLOAD_SESSION_EXPIRED", "Upload session has expired")
    if offset != upload.bytes_uploaded:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "UPLOAD_OFFSET_MISMATCH",
                "message": "Resume from the server-confirmed byte offset",
                "details": {"expectedOffset": upload.bytes_uploaded},
            },
        )
    content = await request.body()
    if not content:
        raise HTTPException(status_code=422, detail="Upload chunk is empty")
    total = int(upload.total_bytes or 0)
    if offset + len(content) > total:
        raise HTTPException(status_code=413, detail="Upload exceeds the declared file size")
    part = _upload_part_path(session_id)
    with part.open("ab") as destination:
        destination.write(content)
    upload.bytes_uploaded += len(content)
    upload.status = "UPLOADED" if upload.bytes_uploaded == total else "UPLOADING"
    asset = db.query(MediaAsset).filter(MediaAsset.id == upload.media_asset_id).first()
    asset.status = upload.status
    uploaded_parts = json.loads(upload.uploaded_parts_json or "[]")
    uploaded_parts.append(
        {"offset": offset, "size": len(content), "checksum": hashlib.sha256(content).hexdigest()}
    )
    upload.uploaded_parts_json = json.dumps(uploaded_parts)
    db.commit()
    return _asset_payload(asset, upload)


@router.post("/media/upload-sessions/{session_id}/finalize")
def finalize_resumable_upload(
    session_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    upload = (
        db.query(UploadSession)
        .filter(UploadSession.id == session_id)
        .with_for_update()
        .first()
    )
    if not upload:
        raise _not_found("Upload session")
    if upload.owner_id != user.id:
        raise _forbidden("You cannot finalize this session")
    asset = db.query(MediaAsset).filter(MediaAsset.id == upload.media_asset_id).first()
    if upload.bytes_uploaded != upload.total_bytes:
        raise _conflict("UPLOAD_INCOMPLETE", "All bytes must be uploaded before finalization")
    part = _upload_part_path(session_id)
    if not part.exists() or part.stat().st_size != upload.total_bytes:
        raise _conflict("UPLOAD_INCOMPLETE", "The resumable upload data is incomplete")
    content = part.read_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    if asset.checksum_sha256 and checksum != asset.checksum_sha256:
        asset.status = "FAILED"
        asset.processing_error = "Checksum mismatch"
        db.commit()
        raise _conflict("CHECKSUM_MISMATCH", "Uploaded media checksum does not match")
    asset.checksum_sha256 = checksum
    asset.status = "PROCESSING"
    db.commit()
    try:
        if asset.media_type == "image":
            processed = process_image_variants(content)
            original_url, original_key = upload_image_bytes(
                processed.original,
                processed.original_content_type,
                processed.original_extension,
                user_id=user.id,
            )
            variants: dict[str, str] = {}
            for name, (data, content_type, extension, _width, _height) in processed.variants.items():
                url, _key = upload_image_bytes(
                    data,
                    content_type,
                    extension,
                    user_id=user.id,
                )
                variants[name] = url
            asset.original_url = original_url
            asset.storage_key = original_key
            asset.thumbnail_url = variants["thumbnail"]
            asset.variants_json = json.dumps(variants)
            asset.width = processed.width
            asset.height = processed.height
        else:
            processed_video = process_video_variants(content)
            original_url, original_key = upload_image_bytes(
                content,
                asset.content_type,
                ".mp4",
                user_id=user.id,
            )
            thumbnail_url, _thumbnail_key = upload_image_bytes(
                processed_video.thumbnail,
                "image/jpeg",
                ".jpg",
                user_id=user.id,
            )
            variants = {}
            for name, data in processed_video.variants.items():
                url, _key = upload_image_bytes(
                    data,
                    "video/mp4",
                    ".mp4",
                    user_id=user.id,
                )
                variants[name] = url
            asset.original_url = original_url
            asset.storage_key = original_key
            asset.thumbnail_url = thumbnail_url
            asset.variants_json = json.dumps(variants)
            asset.width = processed_video.width
            asset.height = processed_video.height
            asset.duration_seconds = processed_video.duration_seconds
        asset.status = "READY"
        asset.moderation_status = "pending"
        asset.processing_error = None
        upload.status = "COMPLETED"
        part.unlink(missing_ok=True)
        db.commit()
        db.refresh(asset)
        return _asset_payload(asset, upload)
    except (MediaValidationError, VideoProcessingError) as exc:
        asset.status = "FAILED"
        asset.processing_error = str(exc)
        upload.status = "PROCESSING_FAILED"
        db.commit()
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MEDIA_PROCESSING_FAILED",
                "message": str(exc),
                "details": {"videoProcessorAvailable": video_processor_available()},
            },
        ) from exc


@router.get("/media/assets/{asset_id}")
def get_media_asset(
    asset_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = db.query(MediaAsset).filter(MediaAsset.id == asset_id).first()
    if not row:
        raise _not_found("Media asset")
    if row.owner_id != user.id and not user.is_admin:
        raise _forbidden("You cannot access this media asset")
    return _asset_payload(row)


@router.post("/media/upload-sessions/{session_id}/complete")
def complete_upload_session(
    session_id: str,
    body: CompleteUploadRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    upload = (
        db.query(UploadSession)
        .filter(UploadSession.id == session_id)
        .with_for_update()
        .first()
    )
    if not upload:
        raise _not_found("Upload session")
    if upload.owner_id != user.id:
        raise _forbidden("You cannot complete this upload")
    asset = db.query(MediaAsset).filter(MediaAsset.id == upload.media_asset_id).first()
    if upload.status == "COMPLETED":
        return _asset_payload(asset, upload)
    if ensure_utc(upload.expires_at) <= utcnow():
        upload.status = "EXPIRED"
        asset.status = "UPLOAD_FAILED"
        db.commit()
        raise _conflict("UPLOAD_SESSION_EXPIRED", "Upload session has expired")
    if upload.status not in {"PENDING_UPLOAD", "UPLOADING", "UPLOAD_FAILED"}:
        raise _conflict("INVALID_UPLOAD_STATE", f"Cannot complete upload in {upload.status}")
    if storage_backend() != "supabase":
        raise _conflict(
            "DIRECT_UPLOAD_UNAVAILABLE",
            "Use the resumable chunk and finalize endpoints for local storage",
        )
    expected_url = supabase_public_url(
        settings.supabase_storage_bucket.strip(),
        asset.storage_key,
    )
    if body.original_url != expected_url:
        raise _conflict(
            "STORAGE_OBJECT_MISMATCH",
            "The completed object does not match this upload session",
        )
    asset.original_url = expected_url
    asset.thumbnail_url = body.thumbnail_url
    asset.width = body.width
    asset.height = body.height
    asset.duration_seconds = body.duration_seconds
    asset.processing_error = None
    upload.bytes_uploaded = upload.total_bytes or 0
    upload.status = "COMPLETED"
    asset.status = "READY" if asset.media_type == "image" else "PROCESSING"
    db.commit()
    db.refresh(asset)
    return _asset_payload(asset, upload)


class MediaModerationRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    reason: str | None = Field(default=None, max_length=2000)


@admin_router.get("/media/assets")
def admin_list_media_assets(
    moderation_status: str | None = Query(default=None, alias="moderationStatus"),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    query = db.query(MediaAsset)
    if moderation_status:
        query = query.filter(MediaAsset.moderation_status == moderation_status)
    return [
        _asset_payload(row)
        for row in query.order_by(MediaAsset.created_at.desc()).limit(500).all()
    ]


@admin_router.post("/media/assets/{asset_id}/moderation")
def admin_moderate_media_asset(
    asset_id: str,
    body: MediaModerationRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    asset = db.query(MediaAsset).filter(MediaAsset.id == asset_id).with_for_update().first()
    if not asset:
        raise _not_found("Media asset")
    if asset.status not in {"READY", "REJECTED"}:
        raise _conflict("MEDIA_NOT_READY", "Only processed media can be moderated")
    asset.moderation_status = "approved" if body.decision == "approve" else "rejected"
    asset.status = "READY" if body.decision == "approve" else "REJECTED"
    asset.processing_error = body.reason if body.decision == "reject" else None
    db.commit()
    db.refresh(asset)
    payload = _asset_payload(asset)
    payload["moderatedBy"] = admin.id
    payload["moderationReason"] = body.reason
    return payload


@router.post("/media/assets/{asset_id}/retry")
def retry_media_processing(
    asset_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    asset = db.query(MediaAsset).filter(MediaAsset.id == asset_id).with_for_update().first()
    if not asset:
        raise _not_found("Media asset")
    if asset.owner_id != user.id and not user.is_admin:
        raise _forbidden("You cannot retry this asset")
    if asset.status not in {
        "FAILED",
        "UPLOAD_FAILED",
        "PROCESSING_FAILED",
        "MODERATION_FAILED",
        "REJECTED",
    }:
        raise _conflict("INVALID_MEDIA_STATE", "This media asset is not retryable")
    if asset.retry_count >= 3:
        raise _conflict("RETRY_LIMIT_REACHED", "Media retry limit reached")
    asset.retry_count += 1
    asset.processing_error = None
    asset.status = "PENDING_UPLOAD" if not asset.original_url else "PROCESSING"
    db.commit()
    db.refresh(asset)
    return _asset_payload(asset)


class CreateOfferRequest(BaseModel):
    negotiated_price: float = Field(alias="negotiatedPrice", gt=0)
    quantity: int = Field(default=1, ge=1, le=99)
    shipping_fee: float = Field(default=0, alias="shippingFee", ge=0)
    expires_in_minutes: int = Field(default=1440, alias="expiresInMinutes", ge=5, le=10080)


def _get_offer_for_participant(db: Session, offer_id: str, user: User) -> PrivateOffer:
    offer = db.query(PrivateOffer).filter(PrivateOffer.id == offer_id).first()
    if not offer:
        raise _not_found("Private offer")
    if user.id not in {offer.buyer_id, offer.seller_id} and not user.is_admin:
        raise _forbidden("You are not a participant in this offer")
    if offer.status in OFFER_ACTIVE_STATES and ensure_utc(offer.expiration_time) <= utcnow():
        offer.status = "EXPIRED"
        _refresh_offer_message(db, offer)
        enqueue_notification(
            db,
            user_id=offer.seller_id,
            role="seller",
            category="order_update",
            notification_type="private_offer_expired",
            title="Private offer expired",
            body="A buyer-specific offer expired without acceptance.",
            title_zh="专属报价已过期",
            body_zh="一个买家专属报价已过期且未被接受。",
            business_type="offer",
            business_id=offer.id,
            deep_link=f"heymarket://chat/{offer.conversation_id}",
            deduplication_key=f"offer:{offer.id}:expired",
        )
        db.commit()
    return offer


@router.post("/conversations/{conversation_id}/offers", status_code=201)
def create_private_offer(
    conversation_id: str,
    body: CreateOfferRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise _not_found("Conversation")
    if conv.seller_id != user.id:
        raise _forbidden("Only the seller can create an offer")
    listing = db.query(Listing).filter(Listing.id == conv.listing_id).first()
    if not listing or listing.seller_id != user.id:
        raise _not_found("Listing")
    if listing.status != "active" or listing.review_status != "approved":
        raise _conflict("LISTING_UNAVAILABLE", "Listing is not available for an offer")
    if body.negotiated_price > listing.price:
        raise HTTPException(status_code=422, detail="Negotiated price cannot exceed listing price")
    total = round(body.negotiated_price * body.quantity + body.shipping_fee, 2)
    offer = PrivateOffer(
        product_id=listing.id,
        seller_id=user.id,
        buyer_id=conv.buyer_id,
        conversation_id=conv.id,
        original_price=listing.price,
        negotiated_price=body.negotiated_price,
        currency=settings.default_charge_currency.upper(),
        quantity=body.quantity,
        shipping_fee=body.shipping_fee,
        total_amount=total,
        expiration_time=utcnow() + timedelta(minutes=body.expires_in_minutes),
    )
    db.add(offer)
    db.flush()
    payload = _offer_payload(offer)
    message = Message(
        conversation_id=conv.id,
        sender_id=user.id,
        text=f"Private offer: A${total:.2f}",
        message_type="private_offer",
        structured_payload_json=json.dumps(payload),
        official_platform_message=False,
    )
    db.add(message)
    conv.last_message_text = message.text
    conv.last_message_at = utcnow()
    conv.buyer_unread += 1
    enqueue_notification(
        db,
        user_id=conv.buyer_id,
        role="buyer",
        category="order_update",
        notification_type="private_offer_created",
        title="New private offer",
        body=f"The seller offered A${total:.2f} for {listing.title[:100]}.",
        title_zh="新的专属报价",
        body_zh=f"卖家为“{(listing.title_zh or listing.title)[:100]}”提供了 A${total:.2f} 的专属报价。",
        business_type="offer",
        business_id=offer.id,
        deep_link=f"heymarket://chat/{conv.id}",
        deduplication_key=f"offer:{offer.id}:created",
    )
    db.commit()
    db.refresh(offer)
    return _offer_payload(offer)


@router.get("/offers/{offer_id}")
def get_private_offer(
    offer_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    offer = _get_offer_for_participant(db, offer_id, user)
    if offer.buyer_id == user.id and offer.status == "PENDING":
        offer.status = "VIEWED"
        _refresh_offer_message(db, offer)
        db.commit()
    return _offer_payload(offer)


@router.post("/offers/{offer_id}/accept")
def accept_private_offer(
    offer_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    offer = (
        db.query(PrivateOffer)
        .filter(PrivateOffer.id == offer_id)
        .with_for_update()
        .first()
    )
    if not offer:
        raise _not_found("Private offer")
    if offer.buyer_id != user.id:
        raise _forbidden("Only the intended buyer can accept this offer")
    if offer.order_id:
        return {"offer": _offer_payload(offer), "orderId": offer.order_id, "idempotent": True}
    if offer.status not in OFFER_ACTIVE_STATES:
        raise _conflict("OFFER_NOT_PENDING", f"Offer is {offer.status.lower()}")
    if ensure_utc(offer.expiration_time) <= utcnow():
        offer.status = "EXPIRED"
        _refresh_offer_message(db, offer)
        db.commit()
        raise _conflict("OFFER_EXPIRED", "The private offer has expired")
    listing = db.query(Listing).filter(Listing.id == offer.product_id).with_for_update().first()
    if not listing or listing.status != "active" or listing.review_status != "approved":
        raise _conflict("LISTING_UNAVAILABLE", "Listing is no longer available")
    paid_order = (
        db.query(Order)
        .filter(
            Order.listing_id == listing.id,
            Order.status.in_(("pendingShip", "pendingReceive", "pendingReview", "completed")),
        )
        .first()
    )
    if paid_order:
        raise _conflict("LISTING_ALREADY_SOLD", "Listing has already been sold")
    order = Order(
        buyer_id=user.id,
        listing_id=listing.id,
        seller_id=offer.seller_id,
        status="pendingPay",
        amount=offer.total_amount,
        amount_minor=int(round(offer.total_amount * 100)),
        charge_currency=offer.currency.lower(),
        delivery_method="meetup",
    )
    db.add(order)
    db.flush()
    offer.status = "CONVERTED_TO_ORDER"
    offer.order_id = order.id
    offer.accepted_at = utcnow()
    _refresh_offer_message(db, offer)
    (
        db.query(PrivateOffer)
        .filter(
            PrivateOffer.product_id == listing.id,
            PrivateOffer.id != offer.id,
            PrivateOffer.status.in_(tuple(OFFER_ACTIVE_STATES)),
        )
        .update(
            {"status": "INVALIDATED", "cancelled_at": utcnow()},
            synchronize_session=False,
        )
    )
    enqueue_notification(
        db,
        user_id=offer.seller_id,
        role="seller",
        category="order_update",
        notification_type="private_offer_accepted",
        title="Private offer accepted",
        body=f"Your private offer was accepted. Order #{order.id} is awaiting payment.",
        title_zh="专属报价已接受",
        body_zh=f"您的专属报价已被接受。订单 #{order.id} 正在等待付款。",
        business_type="order",
        business_id=str(order.id),
        deep_link=f"heymarket://order/{order.id}",
        deduplication_key=f"offer:{offer.id}:accepted",
        mandatory=True,
    )
    db.commit()
    return {"offer": _offer_payload(offer), "orderId": order.id, "idempotent": False}


@router.post("/offers/{offer_id}/reject")
def reject_private_offer(
    offer_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    offer = _get_offer_for_participant(db, offer_id, user)
    if offer.buyer_id != user.id:
        raise _forbidden("Only the intended buyer can reject this offer")
    if offer.status not in OFFER_ACTIVE_STATES:
        raise _conflict("OFFER_NOT_PENDING", f"Offer is {offer.status.lower()}")
    offer.status = "REJECTED"
    offer.cancelled_at = utcnow()
    _refresh_offer_message(db, offer)
    db.commit()
    return _offer_payload(offer)


@router.post("/offers/{offer_id}/cancel")
def cancel_private_offer(
    offer_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    offer = _get_offer_for_participant(db, offer_id, user)
    if offer.seller_id != user.id:
        raise _forbidden("Only the seller can cancel this offer")
    if offer.status not in OFFER_ACTIVE_STATES:
        raise _conflict("OFFER_NOT_PENDING", f"Offer is {offer.status.lower()}")
    offer.status = "CANCELLED"
    offer.cancelled_at = utcnow()
    _refresh_offer_message(db, offer)
    db.commit()
    return _offer_payload(offer)


class CreateShareRequest(BaseModel):
    channel: str | None = Field(default=None, max_length=30)
    campaign_id: str | None = Field(default=None, alias="campaignId", max_length=50)
    expires_in_days: int = Field(default=30, alias="expiresInDays", ge=1, le=365)


class ShareEventRequest(BaseModel):
    event_type: str = Field(
        alias="eventType",
        pattern="^(open|view|favorite|registration|conversation|order|payment)$",
    )
    anonymous_session_id: str | None = Field(default=None, alias="anonymousSessionId")
    business_id: str | None = Field(default=None, alias="businessId", max_length=50)


@router.post("/listings/{listing_id}/shares", status_code=201)
def create_share_link(
    listing_id: int,
    body: CreateShareRequest,
    user: User | None = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if (
        not listing
        or listing.status != "active"
        or listing.review_status != "approved"
        or listing.seller.account_status != "normal"
    ):
        raise _not_found("Listing")
    row = ShareRecord(
        share_token=secrets.token_urlsafe(32),
        product_id=listing.id,
        sharer_user_id=user.id if user else None,
        share_channel=body.channel,
        campaign_id=body.campaign_id,
        expires_at=utcnow() + timedelta(days=body.expires_in_days),
    )
    db.add(row)
    db.commit()
    return {
        "shareId": row.id,
        "token": row.share_token,
        "path": f"/v1/shares/{row.share_token}",
        "deepLink": f"heymarket://listing/{listing.id}?share={row.share_token}",
        "expiresAt": ensure_utc(row.expires_at).isoformat(),
    }


@router.get("/shares/{token}")
def resolve_share_link(token: str, db: Session = Depends(get_db)):
    row = db.query(ShareRecord).filter(ShareRecord.share_token == token).first()
    if not row or row.status != "active":
        raise _not_found("Share link")
    if row.expires_at and ensure_utc(row.expires_at) <= utcnow():
        row.status = "expired"
        db.commit()
        raise _not_found("Share link")
    listing = db.query(Listing).filter(Listing.id == row.product_id).first()
    if (
        not listing
        or listing.status != "active"
        or listing.review_status != "approved"
        or listing.seller.account_status != "normal"
    ):
        raise _not_found("Listing")
    row.access_count += 1
    db.commit()
    return {
        "shareId": row.id,
        "listingId": listing.id,
        "title": listing.title,
        "deepLink": f"heymarket://listing/{listing.id}?share={row.share_token}",
    }


@router.post("/shares/{token}/events", status_code=202)
def record_share_event(
    token: str,
    body: ShareEventRequest,
    user: User | None = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    share = db.query(ShareRecord).filter(ShareRecord.share_token == token).first()
    if not share or share.status != "active":
        raise _not_found("Share link")
    if body.anonymous_session_id:
        anon = (
            db.query(AnonymousSession)
            .filter(AnonymousSession.id == body.anonymous_session_id)
            .first()
        )
        if not anon:
            raise _not_found("Anonymous session")
    event = ShareAttributionEvent(
        share_id=share.id,
        anonymous_session_id=body.anonymous_session_id,
        user_id=user.id if user else None,
        event_type=body.event_type,
        business_id=body.business_id,
    )
    if body.event_type in {"order", "payment"}:
        share.conversion_count += 1
    db.add(event)
    db.commit()
    return {"accepted": True, "eventId": event.id}


class AnonymousSessionRequest(BaseModel):
    device_id: str | None = Field(default=None, alias="deviceId", max_length=500)
    consent_status: str = Field(default="unknown", alias="consentStatus", pattern="^(unknown|granted|denied)$")


@router.post("/anonymous-sessions", status_code=201)
def create_anonymous_session(body: AnonymousSessionRequest, db: Session = Depends(get_db)):
    device_hash = (
        hashlib.sha256(body.device_id.encode("utf-8")).hexdigest() if body.device_id else None
    )
    row = AnonymousSession(
        device_id_hash=device_hash,
        consent_status=body.consent_status,
        expires_at=utcnow() + timedelta(days=90),
    )
    db.add(row)
    db.commit()
    return {"id": row.id, "expiresAt": ensure_utc(row.expires_at).isoformat()}


@router.post("/anonymous-sessions/{session_id}/link")
def link_anonymous_session(
    session_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = db.query(AnonymousSession).filter(AnonymousSession.id == session_id).first()
    if not row:
        raise _not_found("Anonymous session")
    if row.linked_user_id and row.linked_user_id != user.id:
        raise _conflict("SESSION_ALREADY_LINKED", "Anonymous session is already linked")
    row.linked_user_id = user.id
    row.last_seen_at = utcnow()
    db.commit()
    return {"id": row.id, "linkedUserId": user.id}


class PreferenceUpdate(BaseModel):
    user_role_context: str = Field(alias="userRoleContext", pattern="^(buyer|seller|both)$")
    category: str = Field(min_length=1, max_length=40)
    in_app_enabled: bool = Field(default=True, alias="inAppEnabled")
    push_enabled: bool = Field(default=True, alias="pushEnabled")
    sms_enabled: bool = Field(default=False, alias="smsEnabled")


def _preference_payload(row: NotificationPreference) -> dict:
    return {
        "id": row.id,
        "userRoleContext": row.user_role_context,
        "category": row.category,
        "inAppEnabled": row.in_app_enabled,
        "pushEnabled": row.push_enabled,
        "smsEnabled": row.sms_enabled,
        "mandatory": row.mandatory,
    }


@router.get("/notification-preferences")
def list_notification_preferences(
    role: str | None = Query(default=None, pattern="^(buyer|seller|both)$"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    roles = (role,) if role in {"buyer", "seller"} else ("buyer", "seller")
    existing_keys = {
        (row.user_role_context, row.category)
        for row in db.query(NotificationPreference)
        .filter(NotificationPreference.user_id == user.id)
        .all()
    }
    for role_name in roles:
        for category in DEFAULT_NOTIFICATION_CATEGORIES[role_name]:
            if (role_name, category) in existing_keys:
                continue
            db.add(
                NotificationPreference(
                    user_id=user.id,
                    user_role_context=role_name,
                    category=category,
                    in_app_enabled=True,
                    push_enabled=category != "marketing",
                    mandatory=category in MANDATORY_NOTIFICATION_CATEGORIES,
                )
            )
    db.commit()
    query = db.query(NotificationPreference).filter(NotificationPreference.user_id == user.id)
    if role:
        query = query.filter(NotificationPreference.user_role_context == role)
    return [_preference_payload(row) for row in query.order_by(NotificationPreference.category).all()]


@router.put("/notification-preferences")
def update_notification_preference(
    body: PreferenceUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    category = body.category.lower().strip()
    mandatory = category in MANDATORY_NOTIFICATION_CATEGORIES
    if mandatory and not body.in_app_enabled:
        raise _conflict(
            "MANDATORY_NOTIFICATION",
            "This safety or transaction notification cannot be disabled in-app",
        )
    row = (
        db.query(NotificationPreference)
        .filter(
            NotificationPreference.user_id == user.id,
            NotificationPreference.user_role_context == body.user_role_context,
            NotificationPreference.category == category,
        )
        .first()
    )
    if not row:
        row = NotificationPreference(
            user_id=user.id,
            user_role_context=body.user_role_context,
            category=category,
        )
        db.add(row)
    row.mandatory = mandatory
    row.in_app_enabled = True if mandatory else body.in_app_enabled
    row.push_enabled = body.push_enabled
    row.sms_enabled = body.sms_enabled
    db.commit()
    db.refresh(row)
    return _preference_payload(row)


class SupportConversationRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)
    user_role_context: str = Field(alias="userRoleContext", pattern="^(buyer|seller|both)$")
    order_id: int | None = Field(default=None, alias="orderId")


class SupportReplyRequest(BaseModel):
    body: str = Field(min_length=1, max_length=5000)


class AdminOpenConversationRequest(BaseModel):
    user_id: str = Field(alias="userId")
    user_role_context: str = Field(alias="userRoleContext", pattern="^(buyer|seller)$")
    conversation_type: str = Field(
        default="SYSTEM_SERVICE",
        alias="conversationType",
        pattern="^(BUYER_SUPPORT|SELLER_SUPPORT|ORDER_SUPPORT|DISPUTE_SUPPORT|ACCOUNT_REVIEW|SYSTEM_SERVICE)$",
    )
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)
    order_id: int | None = Field(default=None, alias="orderId")


class AdminBroadcastRequest(BaseModel):
    audience_role: str = Field(alias="audienceRole", pattern="^(buyer|seller|both)$")
    user_ids: list[str] | None = Field(default=None, alias="userIds", max_length=1000)
    title: str = Field(min_length=1, max_length=200)
    title_zh: str | None = Field(default=None, alias="titleZh", max_length=200)
    body: str = Field(min_length=1, max_length=5000)
    body_zh: str | None = Field(default=None, alias="bodyZh", max_length=5000)
    deep_link: str | None = Field(default=None, alias="deepLink", max_length=500)


def _support_payload(row: AdminConversation, db: Session) -> dict:
    messages = (
        db.query(AdminSupportMessage)
        .filter(AdminSupportMessage.conversation_id == row.id)
        .order_by(AdminSupportMessage.created_at.asc())
        .all()
    )
    return {
        "id": row.id,
        "type": row.conversation_type,
        "adminId": row.admin_id,
        "userId": row.user_id,
        "userRoleContext": row.user_role_context,
        "orderId": row.order_id,
        "subject": row.subject,
        "status": row.status,
        "messages": [
            {
                "id": item.id,
                "senderId": item.sender_id,
                "senderRole": item.sender_role,
                "body": item.body,
                "officialPlatformMessage": item.official_platform_message,
                "readAt": ensure_utc(item.read_at).isoformat() if item.read_at else None,
                "createdAt": ensure_utc(item.created_at).isoformat(),
            }
            for item in messages
        ],
    }


@router.post("/support/conversations", status_code=201)
def create_support_conversation(
    body: SupportConversationRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    admin = db.query(User).filter(User.is_admin.is_(True), User.account_status == "normal").first()
    if not admin:
        raise _conflict("NO_SUPPORT_ADMIN", "No support administrator is currently available")
    if body.order_id:
        order = db.query(Order).filter(Order.id == body.order_id).first()
        if not order or user.id not in {order.buyer_id, order.seller_id}:
            raise _forbidden("You cannot open support for this order")
    conv = AdminConversation(
        conversation_type="user_support",
        admin_id=admin.id,
        user_id=user.id,
        user_role_context=body.user_role_context,
        order_id=body.order_id,
        subject=body.subject,
        last_message_at=utcnow(),
    )
    db.add(conv)
    db.flush()
    db.add(
        AdminSupportMessage(
            conversation_id=conv.id,
            sender_id=user.id,
            sender_role=body.user_role_context,
            body=body.body,
            official_platform_message=False,
        )
    )
    db.commit()
    return _support_payload(conv, db)


@router.get("/support/conversations")
def list_support_conversations(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(AdminConversation)
        .filter(AdminConversation.user_id == user.id)
        .order_by(AdminConversation.last_message_at.desc())
        .all()
    )
    return [_support_payload(row, db) for row in rows]


@router.post("/support/conversations/{conversation_id}/messages")
def reply_to_support(
    conversation_id: str,
    body: SupportReplyRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = db.query(AdminConversation).filter(AdminConversation.id == conversation_id).first()
    if not conv:
        raise _not_found("Support conversation")
    if conv.user_id != user.id or conv.status == "closed":
        raise _forbidden("You cannot reply to this support conversation")
    db.add(
        AdminSupportMessage(
            conversation_id=conv.id,
            sender_id=user.id,
            sender_role=conv.user_role_context,
            body=body.body,
            official_platform_message=False,
        )
    )
    conv.last_message_at = utcnow()
    db.commit()
    return _support_payload(conv, db)


@admin_router.get("/support/conversations")
def admin_list_support_conversations(
    status: str | None = Query(default=None),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    query = db.query(AdminConversation)
    if status:
        query = query.filter(AdminConversation.status == status)
    return [
        _support_payload(row, db)
        for row in query.order_by(AdminConversation.last_message_at.desc()).all()
    ]


@admin_router.post("/support/conversations", status_code=201)
def admin_open_support_conversation(
    body: AdminOpenConversationRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    recipient = db.query(User).filter(User.id == body.user_id, User.account_status == "normal").first()
    if not recipient:
        raise _not_found("User")
    if body.order_id:
        order = db.query(Order).filter(Order.id == body.order_id).first()
        if not order:
            raise _not_found("Order")
        expected = order.buyer_id if body.user_role_context == "buyer" else order.seller_id
        if expected != recipient.id:
            raise _conflict("ORDER_PARTICIPANT_MISMATCH", "User is not the selected party for this order")
    conv = AdminConversation(
        conversation_type=body.conversation_type,
        admin_id=admin.id,
        user_id=recipient.id,
        user_role_context=body.user_role_context,
        order_id=body.order_id,
        subject=body.subject,
        last_message_at=utcnow(),
    )
    db.add(conv)
    db.flush()
    db.add(
        AdminSupportMessage(
            conversation_id=conv.id,
            sender_id=admin.id,
            sender_role="admin",
            body=body.body,
            official_platform_message=True,
        )
    )
    enqueue_notification(
        db,
        user_id=recipient.id,
        role=body.user_role_context,
        category="moderation" if body.conversation_type == "ACCOUNT_REVIEW" else "platform_notice",
        notification_type="platform_support_message",
        title=body.subject,
        body=body.body[:500],
        title_zh=body.subject,
        body_zh=body.body[:500],
        business_type="support",
        business_id=conv.id,
        deep_link=f"heymarket://support/{conv.id}",
        deduplication_key=f"support:{conv.id}:opened",
        mandatory=True,
    )
    db.commit()
    return _support_payload(conv, db)


@admin_router.post("/announcements", status_code=202)
def create_role_announcement(
    body: AdminBroadcastRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    query = db.query(User.id).filter(User.account_status == "normal", User.is_admin.is_(False))
    if body.user_ids:
        query = query.filter(User.id.in_(set(body.user_ids)))
    elif body.audience_role == "buyer":
        query = query.filter(User.id.in_(db.query(Order.buyer_id).distinct()))
    elif body.audience_role == "seller":
        query = query.filter(User.id.in_(db.query(Listing.seller_id).distinct()))
    recipients = {row[0] for row in query.limit(10000).all()}
    announcement_id = secrets.token_urlsafe(16)
    created = 0
    for user_id in recipients:
        roles = ("buyer", "seller") if body.audience_role == "both" else (body.audience_role,)
        for role in roles:
            created += int(
                enqueue_notification(
                    db,
                    user_id=user_id,
                    role=role,
                    category="platform_notice",
                    notification_type="platform_announcement",
                    title=body.title,
                    body=body.body,
                    title_zh=body.title_zh or body.title,
                    body_zh=body.body_zh or body.body,
                    business_type="announcement",
                    business_id=announcement_id,
                    deep_link=body.deep_link or "heymarket://home",
                    deduplication_key=f"announcement:{announcement_id}:{user_id}:{role}",
                    mandatory=False,
                )
            )
    db.commit()
    return {"announcementId": announcement_id, "recipientCount": len(recipients), "created": created, "createdBy": admin.id}


@admin_router.post("/support/conversations/{conversation_id}/messages")
def admin_reply_to_support(
    conversation_id: str,
    body: SupportReplyRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    conv = db.query(AdminConversation).filter(AdminConversation.id == conversation_id).first()
    if not conv:
        raise _not_found("Support conversation")
    if conv.status == "closed":
        raise _conflict("CONVERSATION_CLOSED", "Support conversation is closed")
    db.add(
        AdminSupportMessage(
            conversation_id=conv.id,
            sender_id=admin.id,
            sender_role="admin",
            body=body.body,
            official_platform_message=True,
        )
    )
    conv.admin_id = admin.id
    conv.last_message_at = utcnow()
    db.commit()
    return _support_payload(conv, db)


class ExposureRuleRequest(BaseModel):
    product_id: int = Field(alias="productId")
    rule_type: str = Field(
        alias="ruleType",
        pattern="^(boost|suppress|pin|exclude|regional|category)$",
    )
    exposure_weight: float = Field(default=1.0, alias="exposureWeight", ge=0, le=100)
    target_region: str | None = Field(default=None, alias="targetRegion", max_length=100)
    target_category: str | None = Field(default=None, alias="targetCategory", max_length=50)
    start_time: datetime | None = Field(default=None, alias="startTime")
    end_time: datetime | None = Field(default=None, alias="endTime")
    reason: str | None = Field(default=None, max_length=2000)


def _exposure_payload(row: ExposureRule) -> dict:
    return {
        "id": row.id,
        "productId": row.product_id,
        "ruleType": row.rule_type,
        "exposureWeight": row.exposure_weight,
        "targetRegion": row.target_region,
        "targetCategory": row.target_category,
        "startTime": ensure_utc(row.start_time).isoformat() if row.start_time else None,
        "endTime": ensure_utc(row.end_time).isoformat() if row.end_time else None,
        "reason": row.reason,
        "status": row.status,
    }


@admin_router.post("/exposure-rules", status_code=201)
def create_exposure_rule(
    body: ExposureRuleRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    listing = db.query(Listing).filter(Listing.id == body.product_id).first()
    if not listing:
        raise _not_found("Listing")
    if body.start_time and body.end_time and body.end_time <= body.start_time:
        raise HTTPException(status_code=422, detail="endTime must be after startTime")
    row = ExposureRule(
        product_id=body.product_id,
        rule_type=body.rule_type,
        exposure_weight=body.exposure_weight,
        target_region=body.target_region,
        target_category=body.target_category,
        start_time=body.start_time,
        end_time=body.end_time,
        reason=body.reason,
        created_by=admin.id,
    )
    db.add(row)
    enqueue_notification(
        db,
        user_id=listing.seller_id,
        role="seller",
        category="platform_notice",
        notification_type="listing_exposure_adjusted",
        title="Listing exposure updated",
        body=f"Exposure settings changed for {listing.title[:140]}.",
        title_zh="商品曝光设置已更新",
        body_zh=f"“{(listing.title_zh or listing.title)[:140]}”的曝光设置已更新。",
        business_type="listing",
        business_id=str(listing.id),
        deep_link=f"heymarket://listing/{listing.id}",
        deduplication_key=f"exposure:{row.id}:seller",
    )
    db.commit()
    return _exposure_payload(row)


@admin_router.get("/exposure-rules")
def list_exposure_rules(
    status: str | None = Query(default=None),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    query = db.query(ExposureRule)
    if status:
        query = query.filter(ExposureRule.status == status)
    return [_exposure_payload(row) for row in query.order_by(ExposureRule.created_at.desc()).all()]


@admin_router.delete("/exposure-rules/{rule_id}")
def deactivate_exposure_rule(
    rule_id: str,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = db.query(ExposureRule).filter(ExposureRule.id == rule_id).first()
    if not row:
        raise _not_found("Exposure rule")
    row.status = "inactive"
    db.commit()
    return _exposure_payload(row)
