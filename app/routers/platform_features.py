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
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
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
    Favorite,
    Listing,
    MediaAsset,
    Message,
    NotificationPreference,
    Order,
    PendingAction,
    PrivateOffer,
    ShareAttributionEvent,
    ShareRecord,
    UploadSession,
    User,
    UserSettings,
    ensure_utc,
    utcnow,
)
from app.notification_jobs import enqueue_notification
from app.config import settings
from app.media_processing import MediaValidationError, process_image_variants
from app.media_security import scan_media_for_threats
from app.storage import (
    create_signed_upload,
    delete_storage_object,
    download_storage_object,
    storage_backend,
    supabase_public_url,
    upload_bytes_at_key,
    upload_file_at_key,
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
SUPPORTED_NOTIFICATION_CATEGORIES = frozenset(
    category
    for categories in DEFAULT_NOTIFICATION_CATEGORIES.values()
    for category in categories
)
ALLOWED_MEDIA_TYPES = {"image", "video"}
ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
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
        "ownerId": row.owner_id,
        "listingId": row.listing_id,
        "mediaType": row.media_type,
        "status": row.status,
        "moderationStatus": row.moderation_status,
        "contentType": row.content_type,
        "fileSize": row.file_size,
        "checksumSha256": row.checksum_sha256,
        "sourceUrl": row.source_url if row.owner_id else None,
        "originalUrl": row.original_url,
        "thumbnailUrl": row.thumbnail_url,
        "variants": json.loads(row.variants_json or "{}"),
        "width": row.width,
        "height": row.height,
        "durationSeconds": row.duration_seconds,
        "securityScanStatus": row.security_scan_status,
        "processingError": row.processing_error,
        "retryCount": row.retry_count,
        "automaticRetryCount": row.automatic_retry_count,
        "createdAt": ensure_utc(row.created_at).isoformat(),
        "updatedAt": ensure_utc(row.updated_at).isoformat(),
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
    resumable_preferred: bool = Field(default=False, alias="resumablePreferred")


class CompleteUploadRequest(BaseModel):
    original_url: str = Field(alias="originalUrl", min_length=1, max_length=1000)


def _scan_media_signature(media_type: str, content_type: str, content: bytes) -> None:
    """Reject executable/script payloads and mismatched media before decoding.

    Image decoding and FFprobe remain the authoritative structural validation,
    while this fast signature check prevents obvious executable or HTML uploads
    from reaching those heavier parsers.
    """
    if not content:
        raise MediaValidationError("Media is empty")
    blocked_prefixes = (
        b"MZ",
        b"\x7fELF",
        b"#!",
        b"<html",
        b"<!doctype",
        b"<script",
        b"<?php",
    )
    lowered = content[:32].lstrip().lower()
    if any(lowered.startswith(prefix.lower()) for prefix in blocked_prefixes):
        raise MediaValidationError("Unsafe media content was rejected")

    signatures = {
        "image/jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
        "image/png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/gif": lambda data: data.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": lambda data: data.startswith(b"RIFF") and data[8:12] == b"WEBP",
        "video/mp4": lambda data: len(data) >= 12 and data[4:8] == b"ftyp",
        "video/quicktime": lambda data: len(data) >= 12 and data[4:8] == b"ftyp",
        "video/webm": lambda data: data.startswith(b"\x1a\x45\xdf\xa3"),
    }
    validator = signatures.get(content_type)
    if validator is None or not validator(content):
        raise MediaValidationError(
            f"Uploaded bytes do not match the declared {media_type} content type"
        )


def _delete_staging_object_best_effort(storage_key: str) -> None:
    """Cleanup must not turn a validated upload into a failed user operation."""
    try:
        delete_storage_object(storage_key)
    except HTTPException:
        # The canonical object and derivatives are already server-owned. A
        # transient staging cleanup failure can be handled by storage lifecycle
        # policies without invalidating the completed asset.
        return


def _process_asset_bytes(
    asset: MediaAsset,
    content: bytes,
    *,
    user_id: str,
    retain_existing_original: bool,
    verify_declared: bool = True,
) -> None:
    """Validate real bytes and populate server-generated derivatives."""
    checksum = hashlib.sha256(content).hexdigest()
    if verify_declared and asset.checksum_sha256 and checksum != asset.checksum_sha256:
        raise MediaValidationError("Uploaded media checksum does not match")
    if verify_declared and asset.file_size is not None and len(content) != asset.file_size:
        raise MediaValidationError("Uploaded media size does not match the declared size")
    if not asset.checksum_sha256:
        asset.checksum_sha256 = checksum
    asset.status = "PROCESSING"
    asset.security_scan_status = "pending"
    try:
        _scan_media_signature(asset.media_type, asset.content_type, content)
        scan_media_for_threats(content)
    except MediaValidationError:
        asset.security_scan_status = "failed"
        raise
    asset.security_scan_status = "passed"
    if asset.media_type == "image":
        processed = process_image_variants(content)
        if not retain_existing_original:
            original_url, original_key = upload_image_bytes(
                processed.original,
                processed.original_content_type,
                processed.original_extension,
                user_id=user_id,
            )
            asset.original_url = original_url
            asset.storage_key = original_key
        variants: dict[str, str] = {}
        for name, (data, content_type, extension, _width, _height) in processed.variants.items():
            url, _key = upload_image_bytes(
                data,
                content_type,
                extension,
                user_id=user_id,
            )
            variants[name] = url
        asset.content_type = processed.original_content_type
        asset.thumbnail_url = variants["thumbnail"]
        asset.variants_json = json.dumps(variants)
        asset.width = processed.width
        asset.height = processed.height
        asset.duration_seconds = None
    else:
        processed_video = process_video_variants(content)
        if not retain_existing_original:
            original_url, original_key = upload_image_bytes(
                content,
                asset.content_type,
                ".mp4",
                user_id=user_id,
            )
            asset.original_url = original_url
            asset.storage_key = original_key
        thumbnail_url, _thumbnail_key = upload_image_bytes(
            processed_video.thumbnail,
            "image/jpeg",
            ".jpg",
            user_id=user_id,
        )
        variants: dict[str, str] = {}
        for name, data in processed_video.variants.items():
            url, _key = upload_image_bytes(
                data,
                "video/mp4",
                ".mp4",
                user_id=user_id,
            )
            variants[name] = url
        storage_prefix = settings.supabase_storage_path_prefix.strip().strip("/")
        hls_root = "/".join(
            part
            for part in (
                storage_prefix,
                f"users/{user_id}/media/{asset.id}/hls",
            )
            if part
        )
        for filename, data in processed_video.adaptive_files.items():
            content_type = (
                "application/vnd.apple.mpegurl"
                if filename.endswith(".m3u8")
                else "video/mp2t"
            )
            url, _key = upload_bytes_at_key(
                data,
                content_type,
                f"{hls_root}/{filename}",
            )
            if filename == "master.m3u8":
                variants["adaptive"] = url
        asset.thumbnail_url = thumbnail_url
        asset.variants_json = json.dumps(variants)
        asset.width = processed_video.width
        asset.height = processed_video.height
        asset.duration_seconds = processed_video.duration_seconds
    asset.status = "READY"
    # Signature validation, structural decoding/transcoding and the configured
    # threat scanner above form the automatic moderation gate.  Mark clean
    # media approved so the normal publish workflow can use it immediately;
    # administrators can still review and reject an approved asset later.
    asset.moderation_status = "approved"
    asset.processing_error = None


def _existing_duplicate_asset(
    db: Session,
    asset: MediaAsset,
    content: bytes,
) -> MediaAsset | None:
    """Detect duplicate bytes server-side even when the client omitted a checksum."""
    checksum = hashlib.sha256(content).hexdigest()
    if asset.file_size is not None and len(content) != asset.file_size:
        raise MediaValidationError("Uploaded media size does not match the declared size")
    if asset.checksum_sha256 and checksum != asset.checksum_sha256:
        raise MediaValidationError("Uploaded media checksum does not match")
    asset.checksum_sha256 = checksum
    deduplication_key = f"{asset.owner_id}:{checksum}"
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:deduplication_key))"),
            {"deduplication_key": deduplication_key},
        )
    existing = (
        db.query(MediaAsset)
        .filter(
            MediaAsset.id != asset.id,
            MediaAsset.deduplication_key == deduplication_key,
        )
        .order_by(MediaAsset.created_at.asc())
        .first()
    )
    if not existing:
        # Compatibility for assets created before the deduplication-key
        # migration; the migration backfills these in deployed databases.
        existing = (
            db.query(MediaAsset)
            .filter(
                MediaAsset.id != asset.id,
                MediaAsset.owner_id == asset.owner_id,
                MediaAsset.checksum_sha256 == checksum,
            )
            .order_by(MediaAsset.created_at.asc())
            .first()
        )
        if existing and not existing.deduplication_key:
            existing.deduplication_key = deduplication_key
    if not existing:
        asset.deduplication_key = deduplication_key
    return existing


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
    deduplication_key = f"{user.id}:{checksum}" if checksum else None
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
                MediaAsset.deduplication_key == deduplication_key,
            )
            .first()
        )
        if existing:
            existing_session = (
                db.query(UploadSession)
                .filter(UploadSession.media_asset_id == existing.id)
                .order_by(UploadSession.created_at.desc())
                .first()
            )
            return {**_asset_payload(existing, existing_session), "deduplicated": True}
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
        deduplication_key=deduplication_key,
        storage_key=storage_key,
    )
    db.add(asset)
    try:
        # Flush is inside the conflict handler because the unique invariant is
        # normally checked here, before commit, by PostgreSQL and SQLite.
        db.flush()
        session = UploadSession(
            media_asset_id=asset.id,
            owner_id=user.id,
            total_bytes=body.file_size,
            expires_at=utcnow() + timedelta(hours=1),
        )
        db.add(session)
        db.commit()
    except IntegrityError:
        db.rollback()
        if not deduplication_key:
            raise
        existing = db.query(MediaAsset).filter(
            MediaAsset.deduplication_key == deduplication_key
        ).first()
        if not existing:
            raise
        existing_session = (
            db.query(UploadSession)
            .filter(UploadSession.media_asset_id == existing.id)
            .order_by(UploadSession.created_at.desc())
            .first()
        )
        return {**_asset_payload(existing, existing_session), "deduplicated": True}
    db.refresh(asset)
    db.refresh(session)
    # Signed direct uploads keep normal images off the application server.
    # Large/unstable-network video clients can explicitly choose the backend's
    # offset-checked multipart path so an interruption resumes from the last
    # confirmed chunk instead of restarting the whole file.
    direct_upload = (
        None
        if body.resumable_preferred
        else create_signed_upload(asset.storage_key, asset.content_type)
    )
    return {
        **_asset_payload(asset, session, direct_upload=direct_upload),
        "deduplicated": False,
    }


def _upload_part_path(session_id: str) -> Path:
    root = Path(settings.upload_dir) / ".upload-sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{session_id}.part"


@router.get("/media/upload-sessions/{session_id}")
def get_upload_session(
    session_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    upload = db.query(UploadSession).filter(UploadSession.id == session_id).first()
    if not upload:
        raise _not_found("Upload session")
    if upload.owner_id != user.id and not user.is_admin:
        raise _forbidden("You cannot access this upload session")
    asset = db.query(MediaAsset).filter(MediaAsset.id == upload.media_asset_id).first()
    if not asset:
        raise _not_found("Media asset")
    if (
        upload.status not in {"COMPLETED", "EXPIRED"}
        and ensure_utc(upload.expires_at) <= utcnow()
    ):
        upload.status = "EXPIRED"
        if asset.status not in {"READY", "REJECTED", "DELETED"}:
            asset.status = "UPLOAD_FAILED"
        db.commit()
    return _asset_payload(asset, upload)


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
    part = _upload_part_path(session_id)
    staged_size = part.stat().st_size if part.exists() else 0
    if staged_size != upload.bytes_uploaded:
        asset = db.query(MediaAsset).filter(MediaAsset.id == upload.media_asset_id).first()
        if staged_size > int(upload.total_bytes or 0):
            upload.status = "UPLOAD_FAILED"
            asset.status = "UPLOAD_FAILED"
            db.commit()
            raise _conflict(
                "UPLOAD_STORAGE_CORRUPT",
                "The staged upload is larger than the declared file and must be restarted",
            )
        upload.bytes_uploaded = staged_size
        upload.status = "UPLOADED" if staged_size == upload.total_bytes else "UPLOADING"
        asset.status = upload.status
        db.commit()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "UPLOAD_OFFSET_MISMATCH",
                "message": "Resume from the bytes safely staged by the server",
                "details": {"expectedOffset": staged_size},
            },
        )
    content = await request.body()
    if not content:
        raise HTTPException(status_code=422, detail="Upload chunk is empty")
    total = int(upload.total_bytes or 0)
    if offset + len(content) > total:
        raise HTTPException(status_code=413, detail="Upload exceeds the declared file size")
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
    try:
        original_url, storage_key = upload_file_at_key(
            part,
            asset.content_type,
            asset.storage_key,
        )
        asset.original_url = original_url
        asset.storage_key = storage_key
        asset.source_url = original_url
        asset.source_storage_key = storage_key
        asset.status = "PROCESSING"
        asset.processing_error = None
        upload.status = "PROCESSING"
        upload.bytes_uploaded = int(upload.total_bytes or 0)
        part.unlink(missing_ok=True)
        db.commit()
        db.refresh(asset)
        return _asset_payload(asset, upload)
    except HTTPException:
        asset.status = "UPLOAD_FAILED"
        upload.status = "UPLOAD_FAILED"
        db.commit()
        raise


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
    staging_storage_key = asset.storage_key
    if body.original_url != expected_url:
        raise _conflict(
            "STORAGE_OBJECT_MISMATCH",
            "The completed object does not match this upload session",
        )
    asset.original_url = expected_url
    asset.source_url = expected_url
    asset.source_storage_key = asset.storage_key
    asset.status = "PROCESSING"
    upload.status = "PROCESSING"
    db.commit()
    # The object is now server-owned. Validation, malware scanning, derivative
    # generation, and transcoding run from the durable PROCESSING state in the
    # background worker rather than holding this HTTP request open.
    del staging_storage_key
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
        for row in query.order_by(MediaAsset.created_at.desc()).yield_per(500)
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
    affected_listing_ids: list[int] = []
    if body.decision == "reject":
        asset_urls = {
            value
            for value in (asset.original_url, asset.thumbnail_url)
            if value
        }
        try:
            variants = json.loads(asset.variants_json or "{}")
        except (TypeError, json.JSONDecodeError):
            variants = {}
        if isinstance(variants, dict):
            asset_urls.update(str(value) for value in variants.values() if value)
        seller_listings = db.query(Listing).filter(Listing.seller_id == asset.owner_id).all()
        for listing in seller_listings:
            listing_urls = set(listing.images or [])
            listing_urls.update(listing.videos or [])
            if listing.image_url:
                listing_urls.add(listing.image_url)
            if isinstance(listing.bundle_meta, dict):
                for item in listing.bundle_meta.get("items") or []:
                    if isinstance(item, dict):
                        listing_urls.update(item.get("imageUrls") or [])
                        if item.get("imageUrl"):
                            listing_urls.add(item["imageUrl"])
            if not listing_urls.intersection(asset_urls):
                continue
            listing.review_status = "rejected"
            listing.review_note = body.reason or "A media asset was rejected during moderation."
            affected_listing_ids.append(listing.id)
            enqueue_notification(
                db,
                user_id=listing.seller_id,
                role="seller",
                category="moderation",
                notification_type="media_rejected",
                title="Listing media requires changes",
                body=f'An image or video on "{listing.title[:120]}" was rejected.',
                title_zh="商品媒体需要修改",
                body_zh=f"“{(listing.title_zh or listing.title)[:120]}”中的图片或视频未通过审核。",
                business_type="listing",
                business_id=str(listing.id),
                deep_link=f"heymarket://listing/{listing.id}",
                deduplication_key=f"media:{asset.id}:rejected:listing:{listing.id}",
                mandatory=True,
            )
    db.commit()
    db.refresh(asset)
    payload = _asset_payload(asset)
    payload["moderatedBy"] = admin.id
    payload["moderationReason"] = body.reason
    payload["affectedListingIds"] = affected_listing_ids
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
    }:
        raise _conflict("INVALID_MEDIA_STATE", "This media asset is not retryable")
    if asset.retry_count >= 3:
        raise _conflict("RETRY_LIMIT_REACHED", "Media retry limit reached")
    if not asset.original_url:
        asset.retry_count += 1
        asset.automatic_retry_count = 0
        asset.processing_error = None
        asset.status = "PENDING_UPLOAD"
        db.commit()
        db.refresh(asset)
        return _asset_payload(asset)
    asset.retry_count += 1
    asset.automatic_retry_count = 0
    asset.processing_error = None
    asset.status = "PROCESSING"
    upload = (
        db.query(UploadSession)
        .filter(UploadSession.media_asset_id == asset.id)
        .order_by(UploadSession.created_at.desc())
        .first()
    )
    if upload:
        upload.status = "PROCESSING"
    db.commit()
    db.refresh(asset)
    return _asset_payload(asset, upload)


def process_queued_media(db: Session, *, limit: int = 2) -> int:
    """Process durable media jobs outside request/response handlers.

    PROCESSING is the durable queue state. A restart leaves those rows intact,
    and the next worker pass resumes them from the server-owned source object.
    """
    completed = 0
    for _ in range(max(1, limit)):
        now = utcnow()
        asset = (
            db.query(MediaAsset)
            .filter(
                MediaAsset.status == "PROCESSING",
                MediaAsset.original_url.isnot(None),
                (
                    MediaAsset.processing_lease_until.is_(None)
                    | (MediaAsset.processing_lease_until <= now)
                ),
            )
            .order_by(MediaAsset.updated_at.asc())
            .with_for_update(skip_locked=True)
            .first()
        )
        if not asset:
            break
        lease_token = secrets.token_urlsafe(24)
        asset.processing_lease_token = lease_token
        asset.processing_lease_until = now + timedelta(minutes=15)
        db.commit()
        db.refresh(asset)
        upload = (
            db.query(UploadSession)
            .filter(UploadSession.media_asset_id == asset.id)
            .order_by(UploadSession.created_at.desc())
            .first()
        )
        staging_storage_key = asset.source_storage_key or asset.storage_key
        max_bytes = MAX_IMAGE_BYTES if asset.media_type == "image" else MAX_VIDEO_BYTES
        try:
            content = download_storage_object(staging_storage_key, max_bytes=max_bytes)
            duplicate = _existing_duplicate_asset(db, asset, content)
            if duplicate:
                asset.status = "DELETED"
                asset.processing_error = f"DUPLICATE_OF:{duplicate.id}"
                asset.processing_lease_token = None
                asset.processing_lease_until = None
                if upload:
                    upload.status = "COMPLETED"
                _delete_staging_object_best_effort(staging_storage_key)
                db.commit()
                completed += 1
                continue
            _process_asset_bytes(
                asset,
                content,
                user_id=asset.owner_id,
                retain_existing_original=False,
            )
            if (
                not settings.retain_original_media
                and asset.storage_key != staging_storage_key
            ):
                _delete_staging_object_best_effort(staging_storage_key)
            if upload:
                upload.status = "COMPLETED"
                upload.bytes_uploaded = int(asset.file_size or upload.total_bytes or 0)
            asset.processing_lease_token = None
            asset.processing_lease_until = None
            db.commit()
            completed += 1
        except (OSError, HTTPException) as exc:
            # Object-storage, scanner, and other infrastructure failures can be
            # transient. Keep the durable job queued for a bounded number of
            # automatic attempts; validation/transcoding failures below remain
            # terminal until the owner explicitly retries or replaces the file.
            asset.automatic_retry_count += 1
            retryable_http = isinstance(exc, HTTPException) and exc.status_code >= 500
            automatically_retry = isinstance(exc, OSError) or retryable_http
            asset.status = (
                "PROCESSING"
                if automatically_retry and asset.automatic_retry_count < 3
                else "FAILED"
            )
            asset.processing_error = (
                str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
            )
            asset.processing_lease_token = None
            asset.processing_lease_until = (
                utcnow() + timedelta(seconds=5 * (2 ** (asset.automatic_retry_count - 1)))
                if asset.status == "PROCESSING"
                else None
            )
            if upload:
                upload.status = (
                    "PROCESSING"
                    if asset.status == "PROCESSING"
                    else "PROCESSING_FAILED"
                )
            db.commit()
        except (MediaValidationError, VideoProcessingError) as exc:
            asset.status = "FAILED"
            asset.processing_error = str(exc)
            asset.processing_lease_token = None
            asset.processing_lease_until = None
            if upload:
                upload.status = "PROCESSING_FAILED"
            db.commit()
    return completed


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
    if not listing.negotiable:
        raise _conflict("LISTING_NOT_NEGOTIABLE", "This listing does not accept private offers")
    if body.quantity != 1:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UNSUPPORTED_QUANTITY",
                "message": "This listing represents one item, so offer quantity must be 1",
                "details": {},
            },
        )
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
        text=f"Private offer: {offer.currency} {total:.2f}",
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
        body=f"The seller offered {offer.currency} {total:.2f} for {listing.title[:100]}.",
        title_zh="新的专属报价",
        body_zh=f"卖家为“{(listing.title_zh or listing.title)[:100]}”提供了 {offer.currency} {total:.2f} 的专属报价。",
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
    owning_order = (
        db.query(Order)
        .filter(
            Order.listing_id == listing.id,
            (
                Order.status.in_(
                    (
                        "pendingShip",
                        "pendingService",
                        "pendingReceive",
                        "pendingReview",
                        "completed",
                    )
                )
            )
            | ((Order.status == "pendingPay") & Order.private_offer_id.is_not(None)),
        )
        .first()
    )
    if owning_order:
        raise _conflict("LISTING_ALREADY_RESERVED", "Listing is already sold or reserved")
    order = Order(
        buyer_id=user.id,
        listing_id=listing.id,
        seller_id=offer.seller_id,
        status="pendingPay",
        amount=offer.total_amount,
        amount_minor=int(round(offer.total_amount * 100)),
        charge_currency=offer.currency.lower(),
        delivery_method="express" if offer.shipping_fee > 0 else "meetup",
        private_offer_id=offer.id,
    )
    db.add(order)
    db.flush()
    offer.status = "CONVERTED_TO_ORDER"
    offer.order_id = order.id
    offer.accepted_at = utcnow()
    _refresh_offer_message(db, offer)
    competing_offers = (
        db.query(PrivateOffer)
        .filter(
            PrivateOffer.product_id == listing.id,
            PrivateOffer.id != offer.id,
            PrivateOffer.status.in_(tuple(OFFER_ACTIVE_STATES)),
        )
        .all()
    )
    invalidated_at = utcnow()
    for competing_offer in competing_offers:
        competing_offer.status = "INVALIDATED"
        competing_offer.cancelled_at = invalidated_at
        _refresh_offer_message(db, competing_offer)
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
    public_origin = settings.public_app_url.strip().rstrip("/")
    deep_link = (
        f"{public_origin}/s/{row.share_token}"
        if public_origin
        else f"heymarket://listing/{listing.id}?share={row.share_token}"
    )
    return {
        "shareId": row.id,
        "token": row.share_token,
        "path": f"/v1/shares/{row.share_token}",
        "deepLink": deep_link,
        "expiresAt": ensure_utc(row.expires_at).isoformat(),
    }


@router.get("/shares/{token}")
def resolve_share_link(token: str, db: Session = Depends(get_db)):
    row = (
        db.query(ShareRecord)
        .filter(ShareRecord.share_token == token)
        .with_for_update()
        .first()
    )
    if not row or row.status != "active":
        raise _not_found("Share link")
    if row.expires_at and ensure_utc(row.expires_at) <= utcnow():
        row.status = "expired"
        db.commit()
        raise _not_found("Share link")
    if row.access_count >= settings.share_max_access_count:
        row.status = "suspended"
        db.commit()
        raise HTTPException(
            status_code=429,
            detail={
                "code": "SHARE_ACCESS_LIMIT_REACHED",
                "message": "Share link has been suspended because its access limit was reached",
                "details": {},
            },
        )
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
    share = (
        db.query(ShareRecord)
        .filter(ShareRecord.share_token == token)
        .with_for_update()
        .first()
    )
    if not share or share.status != "active":
        raise _not_found("Share link")
    if share.expires_at and ensure_utc(share.expires_at) <= utcnow():
        share.status = "expired"
        db.commit()
        raise HTTPException(
            status_code=410,
            detail={"code": "SHARE_EXPIRED", "message": "Share link has expired", "details": {}},
        )
    listing = db.query(Listing).filter(Listing.id == share.product_id).first()
    if (
        not listing
        or listing.status != "active"
        or listing.review_status != "approved"
        or listing.seller.account_status != "normal"
    ):
        raise _not_found("Listing")
    authenticated_events = {"favorite", "registration", "conversation", "order", "payment"}
    if body.event_type in authenticated_events and not user:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "AUTHENTICATION_REQUIRED",
                "message": "Authentication is required for this attribution event",
                "details": {},
            },
        )
    if not user and not body.anonymous_session_id:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ANONYMOUS_SESSION_REQUIRED",
                "message": "An anonymous session is required for guest attribution",
                "details": {},
            },
        )
    if body.event_type == "registration" and not body.anonymous_session_id:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "REGISTRATION_SESSION_REQUIRED",
                "message": "Registration attribution requires the consented anonymous session that preceded login",
                "details": {},
            },
        )
    anon: AnonymousSession | None = None
    attribution_session_id = body.anonymous_session_id
    if body.anonymous_session_id:
        anon = (
            db.query(AnonymousSession)
            .filter(AnonymousSession.id == body.anonymous_session_id)
            .first()
        )
        if not anon or (anon.expires_at and ensure_utc(anon.expires_at) <= utcnow()):
            raise _not_found("Anonymous session")
        if anon.linked_user_id:
            if not user:
                raise _conflict(
                    "SESSION_ALREADY_LINKED",
                    "A linked anonymous session cannot be used by a guest",
                )
            if anon.linked_user_id != user.id:
                raise _conflict("SESSION_USER_MISMATCH", "Anonymous session belongs to another user")
        elif user:
            raise _conflict(
                "SESSION_NOT_LINKED",
                "Link the anonymous session to the authenticated account before attribution",
            )
        anon.last_seen_at = utcnow()
        if anon.consent_status != "granted":
            db.commit()
            return {
                "accepted": False,
                "recorded": False,
                "reason": "analytics_consent_required",
            }

    if user:
        privacy_settings = (
            db.query(UserSettings)
            .filter(UserSettings.user_id == user.id)
            .first()
        )
        if privacy_settings and not privacy_settings.personalization:
            if anon:
                db.commit()
            return {
                "accepted": False,
                "recorded": False,
                "reason": "personalization_disabled",
            }

    if body.event_type == "favorite":
        if not body.business_id or body.business_id != str(listing.id):
            raise _conflict("ATTRIBUTION_MISMATCH", "Favorite does not belong to the shared listing")
        exists_for_user = (
            db.query(Favorite.id)
            .filter(Favorite.user_id == user.id, Favorite.listing_id == listing.id)
            .first()
        )
        if not exists_for_user:
            raise _conflict("BUSINESS_EVENT_NOT_FOUND", "Favorite was not found")
    elif body.event_type == "registration":
        if ensure_utc(user.created_at) < ensure_utc(share.created_at):
            raise _conflict(
                "REGISTRATION_NOT_ATTRIBUTABLE",
                "This account existed before the product was shared",
            )
    elif body.event_type == "conversation":
        conversation = (
            db.query(Conversation)
            .filter(
                Conversation.id == body.business_id,
                Conversation.listing_id == listing.id,
                ((Conversation.buyer_id == user.id) | (Conversation.seller_id == user.id)),
            )
            .first()
        )
        if not conversation:
            raise _conflict("BUSINESS_EVENT_NOT_FOUND", "Conversation was not found")
    elif body.event_type in {"order", "payment"}:
        try:
            order_id = int(body.business_id or "")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="businessId must be an order ID") from exc
        order = (
            db.query(Order)
            .filter(
                Order.id == order_id,
                Order.listing_id == listing.id,
                Order.buyer_id == user.id,
            )
            .first()
        )
        if not order:
            raise _conflict("BUSINESS_EVENT_NOT_FOUND", "Order was not found")
        if body.event_type == "payment" and not (
            order.payment_status in {"succeeded", "paid"}
            or order.status
            in {"pendingShip", "pendingService", "pendingReceive", "pendingReview", "completed"}
        ):
            raise _conflict("PAYMENT_NOT_CONFIRMED", "Payment has not been confirmed")

    attribution_identity = user.id if user else attribution_session_id or "guest"
    deduplication_key = hashlib.sha256(
        "|".join(
            (
                share.id,
                body.event_type,
                attribution_identity,
                body.business_id or "",
            )
        ).encode("utf-8")
    ).hexdigest()
    existing = (
        db.query(ShareAttributionEvent)
        .filter(
            ShareAttributionEvent.deduplication_key == deduplication_key,
        )
        .first()
    )
    if existing:
        return {"accepted": True, "eventId": existing.id, "idempotent": True}
    event = ShareAttributionEvent(
        share_id=share.id,
        anonymous_session_id=attribution_session_id,
        user_id=user.id if user else None,
        event_type=body.event_type,
        business_id=body.business_id,
        deduplication_key=deduplication_key,
    )
    db.add(event)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.query(ShareAttributionEvent).filter(
            ShareAttributionEvent.deduplication_key == deduplication_key
        ).first()
        if not existing:
            raise
        return {"accepted": True, "eventId": existing.id, "idempotent": True}
    if body.event_type == "payment":
        share.conversion_count += 1
    db.commit()
    return {"accepted": True, "eventId": event.id, "idempotent": False}


class AnonymousSessionRequest(BaseModel):
    device_id: str | None = Field(default=None, alias="deviceId", max_length=500)
    consent_status: str = Field(default="unknown", alias="consentStatus", pattern="^(unknown|granted|denied)$")


class AnonymousConsentRequest(BaseModel):
    consent_status: str = Field(
        alias="consentStatus",
        pattern="^(granted|denied)$",
    )


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
    return {
        "id": row.id,
        "consentStatus": row.consent_status,
        "expiresAt": ensure_utc(row.expires_at).isoformat(),
    }


@router.patch("/anonymous-sessions/{session_id}/consent")
def update_anonymous_session_consent(
    session_id: str,
    body: AnonymousConsentRequest,
    db: Session = Depends(get_db),
):
    row = (
        db.query(AnonymousSession)
        .filter(AnonymousSession.id == session_id)
        .with_for_update()
        .first()
    )
    if not row:
        raise _not_found("Anonymous session")
    if row.expires_at and ensure_utc(row.expires_at) <= utcnow():
        raise _conflict("ANONYMOUS_SESSION_EXPIRED", "Anonymous session has expired")
    if row.linked_user_id:
        raise _conflict(
            "ANONYMOUS_SESSION_ALREADY_LINKED",
            "Consent cannot be changed after anonymous data is associated",
        )
    row.consent_status = body.consent_status
    row.last_seen_at = utcnow()
    db.commit()
    return {"id": row.id, "consentStatus": row.consent_status}


@router.post("/anonymous-sessions/{session_id}/link")
def link_anonymous_session(
    session_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = db.query(AnonymousSession).filter(AnonymousSession.id == session_id).first()
    if not row:
        raise _not_found("Anonymous session")
    if row.expires_at and ensure_utc(row.expires_at) <= utcnow():
        raise _conflict("ANONYMOUS_SESSION_EXPIRED", "Anonymous session has expired")
    if row.linked_user_id and row.linked_user_id != user.id:
        raise _conflict("SESSION_ALREADY_LINKED", "Anonymous session is already linked")
    if row.consent_status != "granted":
        row.last_seen_at = utcnow()
        db.commit()
        return {
            "id": row.id,
            "linkedUserId": None,
            "dataAssociated": False,
            "consentStatus": row.consent_status,
        }
    row.linked_user_id = user.id
    row.last_seen_at = utcnow()
    db.commit()
    return {
        "id": row.id,
        "linkedUserId": user.id,
        "dataAssociated": True,
        "consentStatus": row.consent_status,
    }


class PendingActionRequest(BaseModel):
    action_type: str = Field(alias="actionType", min_length=1, max_length=50)
    return_path: str = Field(alias="returnPath", min_length=1, max_length=500)
    anonymous_session_id: str | None = Field(default=None, alias="anonymousSessionId")


@router.post("/pending-actions", status_code=201)
def create_pending_action(body: PendingActionRequest, db: Session = Depends(get_db)):
    parsed_return_path = urlsplit(body.return_path)
    unsafe_return_path = (
        not body.return_path.startswith("/")
        or body.return_path.startswith("//")
        or "\\" in body.return_path
        or any(ord(character) < 32 for character in body.return_path)
        or bool(parsed_return_path.scheme)
        or bool(parsed_return_path.netloc)
    )
    if unsafe_return_path:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_RETURN_PATH",
                "message": "returnPath must be an application-relative path",
                "details": {},
            },
        )
    if body.anonymous_session_id:
        anonymous = (
            db.query(AnonymousSession)
            .filter(AnonymousSession.id == body.anonymous_session_id)
            .first()
        )
        if not anonymous or (
            anonymous.expires_at and ensure_utc(anonymous.expires_at) <= utcnow()
        ):
            raise _not_found("Anonymous session")
    row = PendingAction(
        anonymous_session_id=body.anonymous_session_id,
        action_type=body.action_type,
        return_path=body.return_path,
        expires_at=utcnow() + timedelta(minutes=30),
    )
    db.add(row)
    db.commit()
    return {
        "id": row.id,
        "actionType": row.action_type,
        "returnPath": row.return_path,
        "expiresAt": ensure_utc(row.expires_at).isoformat(),
    }


@router.post("/pending-actions/{action_id}/consume")
def consume_pending_action(
    action_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = db.query(PendingAction).filter(PendingAction.id == action_id).with_for_update().first()
    if not row:
        raise _not_found("Pending action")
    if row.status == "consumed":
        if row.user_id != user.id:
            raise _forbidden("This pending action belongs to another account")
        return {
            "id": row.id,
            "actionType": row.action_type,
            "returnPath": row.return_path,
            "idempotent": True,
        }
    if ensure_utc(row.expires_at) <= utcnow():
        row.status = "expired"
        db.commit()
        raise _conflict("PENDING_ACTION_EXPIRED", "Pending action has expired")
    if row.anonymous_session_id:
        anonymous = (
            db.query(AnonymousSession)
            .filter(AnonymousSession.id == row.anonymous_session_id)
            .first()
        )
        if anonymous and anonymous.linked_user_id not in {None, user.id}:
            raise _forbidden("This pending action belongs to another account")
        if anonymous and anonymous.consent_status == "granted":
            anonymous.linked_user_id = user.id
            anonymous.last_seen_at = utcnow()
    row.user_id = user.id
    row.status = "consumed"
    row.consumed_at = utcnow()
    db.commit()
    return {
        "id": row.id,
        "actionType": row.action_type,
        "returnPath": row.return_path,
        "idempotent": False,
    }


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
    if category not in SUPPORTED_NOTIFICATION_CATEGORIES:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_NOTIFICATION_CATEGORY",
                "message": "The notification category is not supported",
                "details": {"category": category},
            },
        )
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
        actual_role = "buyer" if order.buyer_id == user.id else "seller"
        if body.user_role_context not in {actual_role, "both"}:
            raise _conflict(
                "ORDER_ROLE_MISMATCH",
                f"You are the {actual_role} for this order",
            )
        role_context = actual_role
        conversation_type = "ORDER_SUPPORT"
    else:
        role_context = body.user_role_context
        conversation_type = (
            "BUYER_SUPPORT"
            if role_context == "buyer"
            else "SELLER_SUPPORT"
            if role_context == "seller"
            else "SYSTEM_SERVICE"
        )
    conv = AdminConversation(
        conversation_type=conversation_type,
        admin_id=admin.id,
        user_id=user.id,
        user_role_context=role_context,
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
            sender_role=role_context,
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
    required_role = {
        "BUYER_SUPPORT": "buyer",
        "SELLER_SUPPORT": "seller",
    }.get(body.conversation_type)
    if required_role and body.user_role_context != required_role:
        raise _conflict(
            "SUPPORT_ROLE_MISMATCH",
            f"{body.conversation_type} must target a {required_role}",
        )
    if body.conversation_type in {"ORDER_SUPPORT", "DISPUTE_SUPPORT"} and not body.order_id:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ORDER_ID_REQUIRED",
                "message": f"orderId is required for {body.conversation_type}",
                "details": {},
            },
        )
    if body.order_id:
        order = db.query(Order).filter(Order.id == body.order_id).first()
        if not order:
            raise _not_found("Order")
        expected = order.buyer_id if body.user_role_context == "buyer" else order.seller_id
        if expected != recipient.id:
            raise _conflict("ORDER_PARTICIPANT_MISMATCH", "User is not the selected party for this order")
        if body.conversation_type == "DISPUTE_SUPPORT" and not (
            order.status == "inDispute" or order.dispute_status == "open"
        ):
            raise _conflict(
                "ORDER_NOT_DISPUTED",
                "Dispute support can only be opened for a disputed order",
            )
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
    base_query = db.query(User.id).filter(
        User.account_status == "normal",
        User.is_admin.is_(False),
    )
    selected_ids = set(body.user_ids or [])
    if selected_ids:
        base_query = base_query.filter(User.id.in_(selected_ids))
    # Do not silently omit recipients once the marketplace exceeds an arbitrary
    # size. SQLAlchemy streams rows in batches while preserving complete audience
    # targeting; the delivery queue still handles channel dispatch separately.
    normal_user_ids = {row[0] for row in base_query.yield_per(1000)}
    seller_query = (
        db.query(Listing.seller_id)
        .join(User, User.id == Listing.seller_id)
        .filter(
            User.account_status == "normal",
            User.is_admin.is_(False),
        )
        .distinct()
    )
    if selected_ids:
        seller_query = seller_query.filter(Listing.seller_id.in_(selected_ids))
    seller_ids = {row[0] for row in seller_query.yield_per(1000)}
    if body.audience_role == "seller":
        recipients = seller_ids
    else:
        # Every marketplace account can act as a buyer, including a newly
        # registered user who has not placed an order yet.
        recipients = normal_user_ids
    announcement_id = secrets.token_urlsafe(16)
    created = 0
    for user_id in recipients:
        if body.audience_role == "both":
            roles = ("buyer", "seller") if user_id in seller_ids else ("buyer",)
        else:
            roles = (body.audience_role,)
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
    message = AdminSupportMessage(
        conversation_id=conv.id,
        sender_id=admin.id,
        sender_role="admin",
        body=body.body,
        official_platform_message=True,
    )
    db.add(message)
    db.flush()
    enqueue_notification(
        db,
        user_id=conv.user_id,
        role=conv.user_role_context,
        category="platform_notice",
        notification_type="platform_support_message",
        title=conv.subject,
        body=body.body[:500],
        title_zh=conv.subject,
        body_zh=body.body[:500],
        business_type="support",
        business_id=conv.id,
        deep_link=f"heymarket://support/{conv.id}",
        deduplication_key=f"support:{conv.id}:message:{message.id}",
        mandatory=True,
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
        "createdBy": row.created_by,
        "status": row.status,
        "createdAt": ensure_utc(row.created_at).isoformat(),
        "updatedAt": ensure_utc(row.updated_at).isoformat(),
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
    if body.rule_type == "regional" and not body.target_region:
        raise HTTPException(status_code=422, detail="targetRegion is required for a regional rule")
    if body.rule_type == "category" and not body.target_category:
        raise HTTPException(status_code=422, detail="targetCategory is required for a category rule")
    if body.rule_type == "suppress" and body.exposure_weight > 1:
        raise HTTPException(status_code=422, detail="A suppress rule weight must be between 0 and 1")
    if body.rule_type in {"boost", "pin"} and body.exposure_weight < 1:
        raise HTTPException(status_code=422, detail="A boost or pin rule weight must be at least 1")
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
    listing = db.query(Listing).filter(Listing.id == row.product_id).first()
    if listing:
        remaining_active = (
            db.query(ExposureRule.id)
            .filter(
                ExposureRule.product_id == row.product_id,
                ExposureRule.id != row.id,
                ExposureRule.status == "active",
            )
            .first()
        )
        restored = remaining_active is None
        enqueue_notification(
            db,
            user_id=listing.seller_id,
            role="seller",
            category="platform_notice",
            notification_type=(
                "listing_exposure_restored" if restored else "listing_exposure_adjusted"
            ),
            title=(
                "Normal listing exposure restored"
                if restored
                else "Listing exposure updated"
            ),
            body=(
                f"Normal algorithmic ranking was restored for {listing.title[:140]}."
                if restored
                else f"One exposure rule was removed from {listing.title[:140]}; other manual rules remain active."
            ),
            title_zh="商品曝光已恢复" if restored else "商品曝光设置已更新",
            body_zh=(
                f"“{(listing.title_zh or listing.title)[:140]}”已恢复正常算法排序。"
                if restored
                else f"“{(listing.title_zh or listing.title)[:140]}”已移除一条曝光规则，仍有其他人工规则生效。"
            ),
            business_type="listing",
            business_id=str(listing.id),
            deep_link=f"heymarket://listing/{listing.id}",
            deduplication_key=f"exposure:{row.id}:deactivated:seller",
        )
    db.commit()
    return _exposure_payload(row)


@admin_router.post("/exposure-rules/listings/{product_id}/restore")
def restore_normal_exposure(
    product_id: int,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    listing = db.query(Listing).filter(Listing.id == product_id).first()
    if not listing:
        raise _not_found("Listing")
    rows = (
        db.query(ExposureRule)
        .filter(
            ExposureRule.product_id == product_id,
            ExposureRule.status == "active",
        )
        .with_for_update()
        .all()
    )
    for row in rows:
        row.status = "inactive"
    if rows:
        restore_signature = hashlib.sha256(
            ":".join(sorted(row.id for row in rows)).encode("utf-8")
        ).hexdigest()[:20]
        enqueue_notification(
            db,
            user_id=listing.seller_id,
            role="seller",
            category="platform_notice",
            notification_type="listing_exposure_restored",
            title="Normal listing exposure restored",
            body=f"Normal algorithmic ranking was restored for {listing.title[:140]}.",
            title_zh="商品曝光已恢复",
            body_zh=f"“{(listing.title_zh or listing.title)[:140]}”已恢复正常算法排序。",
            business_type="listing",
            business_id=str(listing.id),
            deep_link=f"heymarket://listing/{listing.id}",
            deduplication_key=(
                f"exposure:listing:{listing.id}:restore:{restore_signature}"
            ),
        )
    db.commit()
    return {
        "productId": listing.id,
        "deactivatedRuleCount": len(rows),
        "status": "normal",
    }
