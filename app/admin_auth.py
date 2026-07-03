"""Admin authentication guard (PROG-402)."""

from __future__ import annotations

from fastapi import Depends, HTTPException

from app.auth import get_current_user
from app.models import User


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail={"code": "FORBIDDEN", "message": "Admin access required", "details": {}},
        )
    if user.account_status == "banned":
        raise HTTPException(
            status_code=403,
            detail={"code": "FORBIDDEN", "message": "Admin account is banned", "details": {}},
        )
    return user
