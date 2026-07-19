import uuid
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from app.config import settings


def _storage_error(message: str, *, status_code: int = 502, code: str = "STORAGE_UPLOAD_FAILED") -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "details": {}},
    )


def storage_backend() -> str:
    return settings.storage_backend.strip().lower() or "local"


def _local_upload(content: bytes, resolved_type: str, ext: str) -> tuple[str, str]:
    key = f"{uuid.uuid4().hex}{ext}"
    upload_path = Path(settings.upload_dir)
    upload_path.mkdir(parents=True, exist_ok=True)
    dest = upload_path / key
    with open(dest, "wb") as f:
        f.write(content)
    return f"{settings.base_url.rstrip('/')}/uploads/{key}", key


def _supabase_object_path(user_id: int, ext: str) -> str:
    prefix = settings.supabase_storage_path_prefix.strip().strip("/")
    filename = f"{uuid.uuid4().hex}{ext}"
    parts = [part for part in (prefix, f"users/{user_id}", filename) if part]
    return "/".join(parts)


def _quote_object_path(object_path: str) -> str:
    return "/".join(quote(part, safe="") for part in object_path.split("/"))


def supabase_public_url(bucket: str, object_path: str) -> str:
    base = settings.supabase_url.strip().rstrip("/")
    return f"{base}/storage/v1/object/public/{quote(bucket, safe='')}/{_quote_object_path(object_path)}"


def _supabase_upload(content: bytes, resolved_type: str, ext: str, *, user_id: int) -> tuple[str, str]:
    base = settings.supabase_url.strip().rstrip("/")
    service_key = settings.supabase_service_role_key.strip()
    bucket = settings.supabase_storage_bucket.strip()
    if not base or not service_key or not bucket:
        raise _storage_error(
            "Supabase Storage is not configured",
            status_code=503,
            code="SUPABASE_STORAGE_NOT_CONFIGURED",
        )

    object_path = _supabase_object_path(user_id, ext)
    upload_url = f"{base}/storage/v1/object/{quote(bucket, safe='')}/{_quote_object_path(object_path)}"
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": resolved_type,
        "Cache-Control": "3600",
        "x-upsert": "false",
    }
    try:
        response = httpx.post(upload_url, content=content, headers=headers, timeout=30.0)
    except httpx.HTTPError as exc:
        raise _storage_error("Could not reach Supabase Storage") from exc

    if response.status_code not in {200, 201}:
        raise _storage_error(
            "Supabase Storage rejected the upload",
            status_code=502,
            code="SUPABASE_STORAGE_UPLOAD_FAILED",
        )

    return supabase_public_url(bucket, object_path), object_path


def create_signed_upload(object_path: str, content_type: str) -> dict[str, object] | None:
    """Create a temporary direct-upload URL without exposing the service-role key."""
    if storage_backend() != "supabase":
        return None
    base = settings.supabase_url.strip().rstrip("/")
    service_key = settings.supabase_service_role_key.strip()
    bucket = settings.supabase_storage_bucket.strip()
    if not base or not service_key or not bucket:
        raise _storage_error(
            "Supabase Storage is not configured",
            status_code=503,
            code="SUPABASE_STORAGE_NOT_CONFIGURED",
        )
    encoded_bucket = quote(bucket, safe="")
    encoded_path = _quote_object_path(object_path)
    sign_url = f"{base}/storage/v1/object/upload/sign/{encoded_bucket}/{encoded_path}"
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
    }
    try:
        response = httpx.post(sign_url, json={"upsert": False}, headers=headers, timeout=15.0)
    except httpx.HTTPError as exc:
        raise _storage_error("Could not create a signed storage upload") from exc
    if response.status_code not in {200, 201}:
        raise _storage_error(
            "Supabase Storage rejected the signed-upload request",
            code="SUPABASE_SIGNED_UPLOAD_FAILED",
        )
    payload = response.json()
    token = payload.get("token")
    signed_path = payload.get("url") or payload.get("signedURL") or payload.get("signedUrl")
    if signed_path:
        upload_url = (
            str(signed_path)
            if str(signed_path).startswith(("http://", "https://"))
            else f"{base}{str(signed_path) if str(signed_path).startswith('/') else '/' + str(signed_path)}"
        )
    elif token:
        upload_url = (
            f"{base}/storage/v1/object/upload/sign/{encoded_bucket}/{encoded_path}"
            f"?token={quote(str(token), safe='')}"
        )
    else:
        raise _storage_error(
            "Supabase Storage did not return a signed-upload token",
            code="SUPABASE_SIGNED_UPLOAD_INVALID",
        )
    return {
        "method": "PUT",
        "url": upload_url,
        "headers": {"Content-Type": content_type, "x-upsert": "false"},
        "publicUrl": supabase_public_url(bucket, object_path),
    }


def upload_image_bytes(content: bytes, resolved_type: str, ext: str, *, user_id: int) -> tuple[str, str]:
    backend = storage_backend()
    if backend == "supabase":
        return _supabase_upload(content, resolved_type, ext, user_id=user_id)
    if backend == "local":
        return _local_upload(content, resolved_type, ext)
    raise _storage_error(
        f"Unsupported storage backend: {backend}",
        status_code=503,
        code="STORAGE_BACKEND_UNSUPPORTED",
    )
