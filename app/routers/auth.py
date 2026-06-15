from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.auth import (
    create_access_token,
    create_refresh_token,
    get_current_user,
    hash_password,
    normalize_phone,
    revoke_user_refresh_tokens,
    store_refresh_token,
    validate_refresh_token,
    verify_password,
)
from app.catalog_helpers import get_or_create_settings
from app.config import settings
from app.database import get_db
from app.models import User
from app.schemas import AuthTokensDto, AuthUserDto, LoginRequest, RefreshRequest, RegisterRequest
from app.serializers import user_to_dto

router = APIRouter(prefix="/auth", tags=["auth"])


def _issue_tokens(db: Session, user: User) -> AuthTokensDto:
    access = create_access_token(user.id)
    refresh = create_refresh_token()
    store_refresh_token(db, user.id, refresh)
    return AuthTokensDto(
        accessToken=access,
        refreshToken=refresh,
        expiresIn=settings.jwt_access_expire_seconds,
        user=user_to_dto(user),
    )


@router.post("/register", response_model=AuthTokensDto, status_code=201)
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    phone = normalize_phone(body.phone)
    if db.query(User).filter(User.phone == phone).first():
        raise HTTPException(
            status_code=409,
            detail={"code": "PHONE_TAKEN", "message": "Phone number already registered", "details": {}},
        )
    heishi_id = f"HS{phone[-8:]}"
    user = User(
        nickname=body.nickname.strip(),
        phone=phone,
        password_hash=hash_password(body.password),
        heishi_id=heishi_id,
        city="Melbourne",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    get_or_create_settings(db, user.id)
    return _issue_tokens(db, user)


@router.post("/login", response_model=AuthTokensDto)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    phone = normalize_phone(body.phone)
    user = db.query(User).filter(User.phone == phone).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=401,
            detail={"code": "INVALID_CREDENTIALS", "message": "Invalid phone or password", "details": {}},
        )
    return _issue_tokens(db, user)


@router.post("/logout", status_code=204)
def logout(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    revoke_user_refresh_tokens(db, user.id)
    return Response(status_code=204)


@router.post("/refresh", response_model=AuthTokensDto)
def refresh(body: RefreshRequest, db: Session = Depends(get_db)):
    user = validate_refresh_token(db, body.refreshToken)
    if not user:
        raise HTTPException(
            status_code=401,
            detail={"code": "TOKEN_EXPIRED", "message": "Invalid or expired refresh token", "details": {}},
        )
    return _issue_tokens(db, user)


@router.get("/me", response_model=AuthUserDto)
def me(user: User = Depends(get_current_user)):
    return user_to_dto(user)
