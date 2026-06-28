"""Supabase Auth JWT verification and claim helpers."""

from __future__ import annotations

from typing import Any

from jose import JWTError, jwt

from app.config import settings

SUPABASE_JWT_ALGORITHM = "HS256"
SUPABASE_JWT_AUDIENCE = "authenticated"


def decode_supabase_jwt(token: str) -> dict[str, Any] | None:
    """Validate a Supabase-issued access token and return claims."""
    secret = settings.supabase_jwt_secret.strip()
    if not secret:
        return None
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[SUPABASE_JWT_ALGORITHM],
            audience=SUPABASE_JWT_AUDIENCE,
        )
    except JWTError:
        return None
    if payload.get("role") != "authenticated":
        return None
    sub = payload.get("sub")
    if not sub or not isinstance(sub, str):
        return None
    return payload


def phone_from_claims(claims: dict[str, Any]) -> str | None:
    phone = claims.get("phone")
    if isinstance(phone, str) and phone.strip():
        return phone.strip()
    meta = claims.get("user_metadata")
    if isinstance(meta, dict):
        meta_phone = meta.get("phone")
        if isinstance(meta_phone, str) and meta_phone.strip():
            return meta_phone.strip()
    return None