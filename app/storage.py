import uuid
import shutil
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


def _safe_object_path(object_path: str) -> str:
    normalized = object_path.replace("\\", "/").strip("/")
    if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise _storage_error(
            "Invalid storage object path",
            status_code=400,
            code="STORAGE_OBJECT_INVALID",
        )
    return normalized


def _local_upload_at_key(content: bytes, object_path: str) -> tuple[str, str]:
    key = _safe_object_path(object_path)
    upload_path = Path(settings.upload_dir)
    upload_path.mkdir(parents=True, exist_ok=True)
    root = upload_path.resolve()
    dest = (root / key).resolve()
    try:
        dest.relative_to(root)
    except ValueError as exc:
        raise _storage_error(
            "Invalid local storage object path",
            status_code=400,
            code="STORAGE_OBJECT_INVALID",
        ) from exc
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(content)
    return f"{settings.base_url.rstrip('/')}/uploads/{key}", key


def _local_upload(content: bytes, resolved_type: str, ext: str) -> tuple[str, str]:
    return _local_upload_at_key(content, f"{uuid.uuid4().hex}{ext}")


def download_storage_object(object_path: str, *, max_bytes: int) -> bytes:
    """Read a server-owned object for validation or a processing retry.

    Direct-upload completion must never trust dimensions, duration, checksum, or
    even file existence reported by the client. The backend reads the object with
    its own storage credentials and validates the actual bytes.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    backend = storage_backend()
    if backend == "local":
        root = Path(settings.upload_dir).resolve()
        candidate = (root / object_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise _storage_error(
                "Invalid local storage object path",
                status_code=400,
                code="STORAGE_OBJECT_INVALID",
            ) from exc
        if not candidate.is_file():
            raise _storage_error(
                "Storage object was not found",
                status_code=404,
                code="STORAGE_OBJECT_NOT_FOUND",
            )
        if candidate.stat().st_size > max_bytes:
            raise _storage_error(
                "Storage object exceeds the allowed size",
                status_code=413,
                code="STORAGE_OBJECT_TOO_LARGE",
            )
        return candidate.read_bytes()
    if backend != "supabase":
        raise _storage_error(
            f"Unsupported storage backend: {backend}",
            status_code=503,
            code="STORAGE_BACKEND_UNSUPPORTED",
        )

    base = settings.supabase_url.strip().rstrip("/")
    service_key = settings.supabase_service_role_key.strip()
    bucket = settings.supabase_storage_bucket.strip()
    if not base or not service_key or not bucket:
        raise _storage_error(
            "Supabase Storage is not configured",
            status_code=503,
            code="SUPABASE_STORAGE_NOT_CONFIGURED",
        )
    download_url = (
        f"{base}/storage/v1/object/{quote(bucket, safe='')}/"
        f"{_quote_object_path(object_path)}"
    )
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
    }
    try:
        with httpx.stream("GET", download_url, headers=headers, timeout=45.0) as response:
            if response.status_code == 404:
                raise _storage_error(
                    "Storage object was not found",
                    status_code=404,
                    code="STORAGE_OBJECT_NOT_FOUND",
                )
            if response.status_code != 200:
                raise _storage_error(
                    "Supabase Storage rejected the download",
                    code="SUPABASE_STORAGE_DOWNLOAD_FAILED",
                )
            declared_length = response.headers.get("content-length")
            if declared_length and int(declared_length) > max_bytes:
                raise _storage_error(
                    "Storage object exceeds the allowed size",
                    status_code=413,
                    code="STORAGE_OBJECT_TOO_LARGE",
                )
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise _storage_error(
                        "Storage object exceeds the allowed size",
                        status_code=413,
                        code="STORAGE_OBJECT_TOO_LARGE",
                    )
                chunks.append(chunk)
            return b"".join(chunks)
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise _storage_error("Could not read the storage object") from exc


def delete_storage_object(object_path: str) -> None:
    """Delete a server-owned staging object after validation/transcoding."""
    backend = storage_backend()
    if backend == "local":
        root = Path(settings.upload_dir).resolve()
        candidate = (root / object_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise _storage_error(
                "Invalid local storage object path",
                status_code=400,
                code="STORAGE_OBJECT_INVALID",
            ) from exc
        candidate.unlink(missing_ok=True)
        return
    if backend != "supabase":
        raise _storage_error(
            f"Unsupported storage backend: {backend}",
            status_code=503,
            code="STORAGE_BACKEND_UNSUPPORTED",
        )
    base = settings.supabase_url.strip().rstrip("/")
    service_key = settings.supabase_service_role_key.strip()
    bucket = settings.supabase_storage_bucket.strip()
    if not base or not service_key or not bucket:
        raise _storage_error(
            "Supabase Storage is not configured",
            status_code=503,
            code="SUPABASE_STORAGE_NOT_CONFIGURED",
        )
    delete_url = f"{base}/storage/v1/object/{quote(bucket, safe='')}"
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
    }
    try:
        response = httpx.delete(
            delete_url,
            json={"prefixes": [object_path]},
            headers=headers,
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise _storage_error("Could not delete the storage object") from exc
    if response.status_code not in {200, 204}:
        raise _storage_error(
            "Supabase Storage rejected the delete request",
            code="SUPABASE_STORAGE_DELETE_FAILED",
        )


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


def _supabase_upload_at_key(
    content: bytes,
    resolved_type: str,
    object_path: str,
) -> tuple[str, str]:
    base = settings.supabase_url.strip().rstrip("/")
    service_key = settings.supabase_service_role_key.strip()
    bucket = settings.supabase_storage_bucket.strip()
    if not base or not service_key or not bucket:
        raise _storage_error(
            "Supabase Storage is not configured",
            status_code=503,
            code="SUPABASE_STORAGE_NOT_CONFIGURED",
        )
    object_path = _safe_object_path(object_path)
    upload_url = (
        f"{base}/storage/v1/object/{quote(bucket, safe='')}/"
        f"{_quote_object_path(object_path)}"
    )
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": resolved_type,
        "Cache-Control": "3600",
        "x-upsert": "true",
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


def upload_bytes_at_key(
    content: bytes,
    resolved_type: str,
    object_path: str,
) -> tuple[str, str]:
    """Upload a server-generated object at a stable relative key.

    Adaptive video manifests reference sibling playlists and segments, so their
    directory layout must be preserved instead of assigning each file an
    unrelated random key.
    """
    backend = storage_backend()
    if backend == "supabase":
        return _supabase_upload_at_key(content, resolved_type, object_path)
    if backend == "local":
        return _local_upload_at_key(content, object_path)
    raise _storage_error(
        f"Unsupported storage backend: {backend}",
        status_code=503,
        code="STORAGE_BACKEND_UNSUPPORTED",
    )


def upload_file_at_key(
    source_path: Path,
    resolved_type: str,
    object_path: str,
) -> tuple[str, str]:
    """Stream a staged file into object storage without loading it into memory."""
    if not source_path.is_file():
        raise _storage_error(
            "Staged upload was not found",
            status_code=404,
            code="STAGED_UPLOAD_NOT_FOUND",
        )
    key = _safe_object_path(object_path)
    backend = storage_backend()
    if backend == "local":
        upload_root = Path(settings.upload_dir).resolve()
        destination = (upload_root / key).resolve()
        try:
            destination.relative_to(upload_root)
        except ValueError as exc:
            raise _storage_error(
                "Invalid local storage object path",
                status_code=400,
                code="STORAGE_OBJECT_INVALID",
            ) from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source_path.resolve() != destination:
            shutil.copyfile(source_path, destination)
        return f"{settings.base_url.rstrip('/')}/uploads/{key}", key
    if backend != "supabase":
        raise _storage_error(
            f"Unsupported storage backend: {backend}",
            status_code=503,
            code="STORAGE_BACKEND_UNSUPPORTED",
        )

    base = settings.supabase_url.strip().rstrip("/")
    service_key = settings.supabase_service_role_key.strip()
    bucket = settings.supabase_storage_bucket.strip()
    if not base or not service_key or not bucket:
        raise _storage_error(
            "Supabase Storage is not configured",
            status_code=503,
            code="SUPABASE_STORAGE_NOT_CONFIGURED",
        )
    upload_url = (
        f"{base}/storage/v1/object/{quote(bucket, safe='')}/"
        f"{_quote_object_path(key)}"
    )
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": resolved_type,
        "Cache-Control": "3600",
        "x-upsert": "true",
    }
    try:
        with source_path.open("rb") as source:
            response = httpx.post(
                upload_url,
                content=source,
                headers=headers,
                timeout=120.0,
            )
    except (OSError, httpx.HTTPError) as exc:
        raise _storage_error("Could not upload the staged storage object") from exc
    if response.status_code not in {200, 201}:
        raise _storage_error(
            "Supabase Storage rejected the staged upload",
            code="SUPABASE_STORAGE_UPLOAD_FAILED",
        )
    return supabase_public_url(bucket, key), key
