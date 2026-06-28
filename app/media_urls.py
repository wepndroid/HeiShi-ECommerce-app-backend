"""Normalize uploaded media URLs for clients (LDPlayer cannot load localhost or file://)."""

from app.config import settings


def normalize_media_url(url: str | None) -> str | None:
    if not url or not url.strip():
        return None
    trimmed = url.strip()
    if trimmed.startswith(("file://", "content://")):
        return None

    base = settings.base_url.rstrip("/")
    if trimmed.startswith("/uploads/"):
        return f"{base}{trimmed}"

    normalized = trimmed.replace("://localhost:", "://127.0.0.1:")
    if normalized.startswith("http://localhost/"):
        normalized = normalized.replace("http://localhost/", f"{base}/", 1)
    return normalized


def normalize_media_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in urls:
        normalized = normalize_media_url(raw)
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out