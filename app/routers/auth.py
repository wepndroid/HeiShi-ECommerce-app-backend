from datetime import datetime, timezone
import secrets

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth import (
    create_access_token,
    create_refresh_token,
    generate_heishi_id,
    get_current_user,
    hash_password,
    is_valid_au_phone,
    normalize_phone,
    revoke_user_refresh_tokens,
    store_refresh_token,
    validate_refresh_token,
    verify_password,
)
from app.catalog_helpers import get_or_create_settings
from app.config import settings
from app.coupon_service import issue_welcome_coupon
from app.database import get_db
from app.models import PhoneOtp, User
from app.phone_verification import (
    OTP_TTL_SECONDS,
    RESEND_COOLDOWN_SECONDS,
    consume_register_code,
    generate_code,
    issue_register_code,
    resend_allowed_at,
)
from app.schemas import (
    AuthTokensDto,
    AuthUserDto,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    SendRegisterCodeRequest,
    SendRegisterCodeResponse,
    SyncProfileRequest,
    ChangePasswordRequest,
)
from app.serializers import user_to_dto
from app.supabase_auth import decode_supabase_jwt, phone_from_claims
from app.routers.region_safety import REGION_DATA

router = APIRouter(prefix="/auth", tags=["auth"])
supabase_security = HTTPBearer(auto_error=False)

KNOWN_CITY_NAMES = {city.name for region in REGION_DATA for city in region.cities}


def _valid_avatar_url(url: str) -> bool:
    trimmed = url.strip()
    if not trimmed or trimmed.startswith(("file://", "content://")):
        return False
    return trimmed.startswith(("http://", "https://", "/uploads/"))


def _require_supabase_claims(
    credentials: HTTPAuthorizationCredentials | None = Depends(supabase_security),
) -> dict:
    if not settings.supabase_auth_enabled:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "SUPABASE_NOT_CONFIGURED",
                "message": "Supabase Auth is not configured on the server",
                "details": {},
            },
        )
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHORIZED", "message": "Authentication required", "details": {}},
        )
    claims = decode_supabase_jwt(credentials.credentials)
    if not claims:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHORIZED", "message": "Invalid Supabase session", "details": {}},
        )
    return claims


@router.post("/sync-profile", response_model=AuthUserDto)
def sync_profile(
    body: SyncProfileRequest,
    claims: dict = Depends(_require_supabase_claims),
    db: Session = Depends(get_db),
):
    """Create or update public.users after Supabase phone OTP sign-up."""
    user_id = claims["sub"]
    phone_raw = phone_from_claims(claims) or body.phone
    if not phone_raw:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_ERROR",
                "message": "Phone number missing from Supabase session",
                "details": {},
            },
        )
    phone = _require_valid_phone(phone_raw)
    city = _require_valid_city(body.city)

    existing_phone = db.query(User).filter(User.phone == phone, User.id != user_id).first()
    if existing_phone:
        raise HTTPException(
            status_code=409,
            detail={"code": "PHONE_TAKEN", "message": "Phone number already registered", "details": {}},
        )

    avatar_url = body.avatarUrl.strip() if body.avatarUrl else ""
    if avatar_url and not _valid_avatar_url(avatar_url):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_ERROR",
                "message": "Avatar must be an uploaded http(s) or /uploads/ URL",
                "details": {},
            },
        )
    if not avatar_url:
        avatar_url = None

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        user = User(
            id=user_id,
            nickname=body.nickname.strip(),
            phone=phone,
            password_hash=hash_password(secrets.token_urlsafe(32)),
            heishi_id=generate_heishi_id(db, phone),
            city=city,
            phone_verified=True,
            avatar_url=avatar_url,
        )
        db.add(user)
        get_or_create_settings(db, user_id)
        issue_welcome_coupon(db, user_id)
    else:
        user.nickname = body.nickname.strip()
        user.phone = phone
        user.city = city
        user.phone_verified = True
        user.avatar_url = avatar_url

    db.commit()
    db.refresh(user)
    return user_to_dto(user)


def _validation_error(message: str = "Invalid phone format") -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"code": "VALIDATION_ERROR", "message": message, "details": {}},
    )


def _require_valid_phone(raw_phone: str) -> str:
    phone = normalize_phone(raw_phone)
    if not is_valid_au_phone(phone):
        raise _validation_error()
    return phone


def _require_valid_city(raw_city: str) -> str:
    city = raw_city.strip()
    if city not in KNOWN_CITY_NAMES:
        raise HTTPException(
            status_code=422,
            detail={"code": "VALIDATION_ERROR", "message": "Invalid city", "details": {}},
        )
    return city


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


@router.post("/register/send-code", response_model=SendRegisterCodeResponse)
def send_register_code(body: SendRegisterCodeRequest, db: Session = Depends(get_db)):
    """Legacy register OTP — used when Supabase Auth is not configured on the client."""
    phone = _require_valid_phone(body.phone)
    if db.query(User).filter(User.phone == phone).first():
        raise HTTPException(
            status_code=409,
            detail={"code": "PHONE_TAKEN", "message": "Phone number already registered", "details": {}},
        )

    existing = (
        db.query(PhoneOtp)
        .filter(PhoneOtp.phone == phone, PhoneOtp.purpose == "register", PhoneOtp.consumed.is_(False))
        .first()
    )
    if existing is not None:
        allowed_at = resend_allowed_at(existing)
        now = datetime.now(timezone.utc)
        if now < allowed_at:
            wait = int((allowed_at - now).total_seconds())
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "OTP_RATE_LIMIT",
                    "message": f"Please wait {wait}s before requesting another code",
                    "details": {"retryAfter": wait},
                },
            )

    code = generate_code()
    issue_register_code(db, phone, code)
    print(f"[HeyMarket OTP] register {phone} -> {code}")

    return SendRegisterCodeResponse(
        expiresIn=OTP_TTL_SECONDS,
        resendAfter=RESEND_COOLDOWN_SECONDS,
        devCode=code if settings.expose_dev_otp else None,
    )


@router.post("/register", response_model=AuthTokensDto, status_code=201)
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    """Legacy register — used when Supabase Auth is not configured on the client."""
    phone = _require_valid_phone(body.phone)
    city = _require_valid_city(body.city)
    if db.query(User).filter(User.phone == phone).first():
        raise HTTPException(
            status_code=409,
            detail={"code": "PHONE_TAKEN", "message": "Phone number already registered", "details": {}},
        )
    try:
        consume_register_code(db, phone, body.verificationCode.strip())
    except ValueError as exc:
        reason = str(exc)
        if reason == "OTP_EXPIRED":
            raise HTTPException(
                status_code=400,
                detail={"code": "OTP_EXPIRED", "message": "Verification code expired", "details": {}},
            ) from exc
        if reason == "OTP_TOO_MANY_ATTEMPTS":
            raise HTTPException(
                status_code=429,
                detail={"code": "OTP_TOO_MANY_ATTEMPTS", "message": "Too many invalid attempts", "details": {}},
            ) from exc
        raise HTTPException(
            status_code=400,
            detail={"code": "OTP_INVALID", "message": "Invalid verification code", "details": {}},
        ) from exc

    heishi_id = generate_heishi_id(db, phone)
    avatar_raw = body.avatarUrl.strip() if body.avatarUrl else ""
    if avatar_raw and not _valid_avatar_url(avatar_raw):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_ERROR",
                "message": "Avatar must be an uploaded http(s) or /uploads/ URL",
                "details": {},
            },
        )
    user = User(
        nickname=body.nickname.strip(),
        phone=phone,
        password_hash=hash_password(body.password),
        heishi_id=heishi_id,
        city=city,
        avatar_url=avatar_raw or None,
    )
    db.add(user)
    db.flush()
    get_or_create_settings(db, user.id)
    issue_welcome_coupon(db, user.id, user.language)
    db.commit()
    db.refresh(user)
    return _issue_tokens(db, user)


@router.post("/login", response_model=AuthTokensDto)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    phone = _require_valid_phone(body.phone)
    user = db.query(User).filter(User.phone == phone).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=401,
            detail={"code": "INVALID_CREDENTIALS", "message": "Invalid phone or password", "details": {}},
        )
    return _issue_tokens(db, user)


@router.post("/change-password", status_code=204)
def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if len(body.newPassword) < 6:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_ERROR",
                "message": "New password must be at least 6 characters",
                "details": {},
            },
        )
    if not verify_password(body.currentPassword, user.password_hash):
        raise HTTPException(
            status_code=401,
            detail={"code": "INVALID_CREDENTIALS", "message": "Current password is incorrect", "details": {}},
        )
    user.password_hash = hash_password(body.newPassword)
    revoke_user_refresh_tokens(db, user.id)
    db.commit()
    return Response(status_code=204)


@router.post("/logout", status_code=204)
def logout(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.models import DevicePushToken

    revoke_user_refresh_tokens(db, user.id)
    db.query(DevicePushToken).filter(DevicePushToken.user_id == user.id).delete(
        synchronize_session=False
    )
    db.commit()
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
