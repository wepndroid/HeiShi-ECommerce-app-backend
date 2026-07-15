from datetime import datetime, timezone
import secrets

import httpx
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
from app.analytics import record_daily_active_user
from app.catalog_helpers import get_or_create_settings
from app.config import settings
from app.coupon_service import issue_welcome_coupon
from app.database import get_db
from app.models import PhoneOtp, User
from app.phone_verification import (
    OTP_TTL_SECONDS,
    RESEND_COOLDOWN_SECONDS,
    consume_login_code,
    consume_register_code,
    generate_code,
    issue_login_code,
    issue_register_code,
    resend_allowed_at,
)
from app.twilio_otp import TwilioOtpError, send_sms_verification, verify_sms_code
from app.schemas import (
    AuthTokensDto,
    AuthUserDto,
    GoogleDevAuthRequest,
    GoogleAuthRequest,
    LoginRequest,
    LoginOtpRequest,
    RefreshRequest,
    RegisterRequest,
    SendRegisterCodeRequest,
    SendRegisterCodeResponse,
    SyncProfileRequest,
    OAuthProvisionRequest,
    WeChatAuthRequest,
    ChangePasswordRequest,
)
from app.serializers import user_to_dto
from app.supabase_auth import (
    avatar_from_claims,
    decode_supabase_jwt,
    email_from_claims,
    name_from_claims,
    phone_from_claims,
)
from app.routers.region_safety import REGION_DATA

router = APIRouter(prefix="/auth", tags=["auth"])
supabase_security = HTTPBearer(auto_error=False)

KNOWN_CITY_NAMES = {city.name for region in REGION_DATA for city in region.cities}
WECHAT_ACCESS_TOKEN_URL = "https://api.weixin.qq.com/sns/oauth2/access_token"
WECHAT_USERINFO_URL = "https://api.weixin.qq.com/sns/userinfo"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


def _valid_avatar_url(url: str) -> bool:
    trimmed = url.strip()
    if not trimmed or trimmed.startswith(("file://", "content://")):
        return False
    return trimmed.startswith(("http://", "https://", "/uploads/"))


def _wechat_not_configured() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": "WECHAT_NOT_CONFIGURED",
            "message": "WeChat login is not configured",
            "details": {},
        },
    )


def _wechat_error(message: str, *, status_code: int = 400, code: str = "WECHAT_AUTH_FAILED") -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "details": {}},
    )


def _google_not_configured() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": "GOOGLE_NOT_CONFIGURED",
            "message": "Google login is not configured",
            "details": {},
        },
    )


def _google_error(message: str, *, status_code: int = 400, code: str = "GOOGLE_AUTH_FAILED") -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "details": {}},
    )


def _wechat_exchange_code(code: str) -> dict:
    app_id = settings.wechat_open_app_id.strip()
    app_secret = settings.wechat_open_app_secret.strip()
    if not app_id or not app_secret:
        raise _wechat_not_configured()

    try:
        with httpx.Client(timeout=15.0) as client:
            token_response = client.get(
                WECHAT_ACCESS_TOKEN_URL,
                params={
                    "appid": app_id,
                    "secret": app_secret,
                    "code": code.strip(),
                    "grant_type": "authorization_code",
                },
            )
            token_payload = token_response.json()
    except Exception as exc:
        raise _wechat_error(
            "Could not reach WeChat login service",
            status_code=502,
            code="WECHAT_NETWORK_ERROR",
        ) from exc

    if token_payload.get("errcode"):
        raise _wechat_error(str(token_payload.get("errmsg") or "WeChat authorization failed"))

    access_token = token_payload.get("access_token")
    openid = token_payload.get("openid")
    if not access_token or not openid:
        raise _wechat_error("WeChat authorization response is missing openid or access token")

    profile: dict = {}
    try:
        with httpx.Client(timeout=15.0) as client:
            profile_response = client.get(
                WECHAT_USERINFO_URL,
                params={
                    "access_token": access_token,
                    "openid": openid,
                    "lang": "en",
                },
            )
            profile = profile_response.json()
    except Exception:
        profile = {}

    if profile.get("errcode"):
        profile = {}

    unionid = profile.get("unionid") or token_payload.get("unionid")
    return {
        "openid": openid,
        "unionid": unionid,
        "nickname": profile.get("nickname"),
        "avatar_url": profile.get("headimgurl"),
    }


def _google_exchange_id_token(id_token: str) -> dict:
    audiences = set(settings.google_oauth_client_ids)
    if not audiences:
        raise _google_not_configured()

    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(
                GOOGLE_TOKENINFO_URL,
                params={"id_token": id_token.strip()},
            )
            payload = response.json()
    except Exception as exc:
        raise _google_error(
            "Could not reach Google login service",
            status_code=502,
            code="GOOGLE_NETWORK_ERROR",
        ) from exc

    if response.status_code >= 400:
        message = payload.get("error_description") or payload.get("error") or "Google login failed"
        raise _google_error(str(message), status_code=401, code="GOOGLE_TOKEN_INVALID")

    audience = str(payload.get("aud") or "").strip()
    if audience not in audiences:
        raise _google_error("Google token audience mismatch", status_code=401, code="GOOGLE_TOKEN_INVALID")

    issuer = str(payload.get("iss") or "").strip()
    if issuer not in {"accounts.google.com", "https://accounts.google.com"}:
        raise _google_error("Google token issuer mismatch", status_code=401, code="GOOGLE_TOKEN_INVALID")

    sub = str(payload.get("sub") or "").strip()
    if not sub:
        raise _google_error("Google token is missing subject", status_code=401, code="GOOGLE_TOKEN_INVALID")

    exp_raw = str(payload.get("exp") or "").strip()
    if exp_raw.isdigit():
        expires_at = datetime.fromtimestamp(int(exp_raw), tz=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise _google_error("Google token expired", status_code=401, code="GOOGLE_TOKEN_INVALID")

    email = str(payload.get("email") or "").strip().lower() or None
    email_verified = str(payload.get("email_verified") or "").strip().lower() == "true"
    picture = str(payload.get("picture") or "").strip() or None
    if picture and not _valid_avatar_url(picture):
        picture = None

    return {
        "sub": sub,
        "email": email,
        "email_verified": email_verified,
        "name": str(payload.get("name") or "").strip() or None,
        "given_name": str(payload.get("given_name") or "").strip() or None,
        "picture": picture,
        "hosted_domain": str(payload.get("hd") or "").strip() or None,
    }


def _find_wechat_user(db: Session, openid: str, unionid: str | None) -> User | None:
    if unionid:
        user = db.query(User).filter(User.wechat_unionid == unionid).first()
        if user:
            return user
    return db.query(User).filter(User.wechat_openid == openid).first()


def _find_google_user(
    db: Session,
    google_sub: str,
    email: str | None,
    *,
    email_verified: bool,
    hosted_domain: str | None,
) -> User | None:
    user = db.query(User).filter(User.google_sub == google_sub).first()
    if user:
        return user
    if not email or not email_verified:
        return None

    email_is_google_authoritative = email.endswith("@gmail.com") or bool(hosted_domain)
    if not email_is_google_authoritative:
        return None
    return db.query(User).filter(User.email == email).first()


def _apply_google_profile_to_user(user: User, profile: dict) -> None:
    google_sub = profile["sub"]
    email = profile.get("email")
    email_verified = bool(profile.get("email_verified"))
    avatar_url = profile.get("picture")

    user.google_sub = google_sub
    if email and (not user.email or user.email == email):
        user.email = email
    if email_verified:
        user.email_verified = True
    if avatar_url and not user.avatar_url:
        user.avatar_url = avatar_url


def _create_google_user(db: Session, body: GoogleAuthRequest, profile: dict) -> User:
    google_sub = profile["sub"]
    email = profile.get("email")
    nickname = (
        (body.nickname.strip() if body.nickname else None)
        or profile.get("name")
        or profile.get("given_name")
        or (email.split("@")[0] if email else None)
        or "Google user"
    )
    city = _valid_optional_city(body.city)
    heishi_seed = (email or google_sub).replace("@", "").replace(".", "")
    user = User(
        nickname=nickname[:50],
        phone=None,
        email=email,
        password_hash=hash_password(secrets.token_urlsafe(32)),
        heishi_id=generate_heishi_id(db, heishi_seed),
        city=city,
        avatar_url=profile.get("picture"),
        phone_verified=False,
        email_verified=bool(profile.get("email_verified")),
        google_sub=google_sub,
    )
    db.add(user)
    db.flush()
    get_or_create_settings(db, user.id)
    issue_welcome_coupon(db, user.id, user.language)
    return user


def _valid_optional_city(raw_city: str | None) -> str | None:
    if raw_city is None:
        return None
    city = raw_city.strip()
    if not city:
        return None
    if city not in KNOWN_CITY_NAMES:
        raise HTTPException(
            status_code=422,
            detail={"code": "VALIDATION_ERROR", "message": "Invalid city", "details": {}},
        )
    return city


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


@router.post("/oauth/provision", response_model=AuthUserDto)
def oauth_provision(
    body: OAuthProvisionRequest | None = None,
    claims: dict = Depends(_require_supabase_claims),
    db: Session = Depends(get_db),
):
    """Create-or-return the app profile for a Supabase OAuth (Google/Apple/WeChat) session.

    Unlike /sync-profile this requires NO phone — OAuth identities are email-based. The
    display name, email, and avatar default from the provider's JWT claims, so a Google
    sign-in provisions an app user with no manual onboarding. Idempotent by Supabase sub.
    """
    user_id = claims["sub"]
    existing = db.query(User).filter(User.id == user_id).first()
    if existing is not None:
        return user_to_dto(existing)

    email = email_from_claims(claims)
    phone = phone_from_claims(claims)  # normally None for Google/Apple
    nickname = (
        (body.nickname.strip() if body and body.nickname else None)
        or name_from_claims(claims)
        or (email.split("@")[0] if email else None)
        or "New user"
    )
    avatar_url = avatar_from_claims(claims)
    city = body.city.strip() if body and body.city else None

    if phone:
        clash = db.query(User).filter(User.phone == phone, User.id != user_id).first()
        if clash:
            raise HTTPException(
                status_code=409,
                detail={"code": "PHONE_TAKEN", "message": "Phone number already registered", "details": {}},
            )

    user = User(
        id=user_id,
        nickname=nickname[:50],
        phone=phone,
        email=email,
        password_hash=hash_password(secrets.token_urlsafe(32)),
        heishi_id=generate_heishi_id(db, phone or user_id.replace("-", "")),
        city=city,
        avatar_url=avatar_url,
        phone_verified=bool(phone),
    )
    db.add(user)
    get_or_create_settings(db, user_id)
    issue_welcome_coupon(db, user_id)
    db.commit()
    db.refresh(user)
    return user_to_dto(user)


@router.post("/wechat", response_model=AuthTokensDto)
def wechat_login(body: WeChatAuthRequest, db: Session = Depends(get_db)):
    """Sign in or register via native WeChat Open Platform authorization code.

    The mobile app obtains a one-time WeChat ``code`` from the native SDK. The
    backend exchanges that code with WeChat, stores openid/unionid, and issues
    the same HeyMarket JWT session used by phone registration/login.
    """

    profile = _wechat_exchange_code(body.code)
    openid = profile["openid"]
    unionid = profile.get("unionid")
    user = _find_wechat_user(db, openid, unionid)
    avatar_url = profile.get("avatar_url")
    if avatar_url and not _valid_avatar_url(avatar_url):
        avatar_url = None

    if user is not None:
        user.wechat_openid = openid
        if unionid:
            user.wechat_unionid = unionid
        user.wechat_bound = True
        if avatar_url and not user.avatar_url:
            user.avatar_url = avatar_url
        db.commit()
        db.refresh(user)
        return _issue_tokens(db, user)

    nickname = (
        (body.nickname.strip() if body.nickname else None)
        or profile.get("nickname")
        or "WeChat user"
    )
    city = _valid_optional_city(body.city)
    heishi_seed = unionid or openid
    user = User(
        nickname=nickname[:50],
        phone=None,
        email=None,
        password_hash=hash_password(secrets.token_urlsafe(32)),
        heishi_id=generate_heishi_id(db, heishi_seed),
        city=city,
        avatar_url=avatar_url,
        phone_verified=False,
        wechat_bound=True,
        wechat_openid=openid,
        wechat_unionid=unionid,
    )
    db.add(user)
    db.flush()
    get_or_create_settings(db, user.id)
    issue_welcome_coupon(db, user.id, user.language)
    db.commit()
    db.refresh(user)
    return _issue_tokens(db, user)


@router.post("/google/login", response_model=AuthTokensDto)
def google_login(body: GoogleAuthRequest, db: Session = Depends(get_db)):
    """Sign in an existing account via native Google Sign-In ID token."""

    profile = _google_exchange_id_token(body.idToken)
    google_sub = profile["sub"]
    email = profile.get("email")
    email_verified = bool(profile.get("email_verified"))
    user = _find_google_user(
        db,
        google_sub,
        email,
        email_verified=email_verified,
        hosted_domain=profile.get("hosted_domain"),
    )

    if user is None:
        raise _google_error(
            "Google account is not registered",
            status_code=404,
            code="GOOGLE_ACCOUNT_NOT_REGISTERED",
        )

    _apply_google_profile_to_user(user, profile)
    db.commit()
    db.refresh(user)
    return _issue_tokens(db, user)


@router.post("/google/register", response_model=AuthTokensDto)
def google_register(body: GoogleAuthRequest, db: Session = Depends(get_db)):
    """Register a new account via native Google Sign-In ID token."""

    profile = _google_exchange_id_token(body.idToken)
    google_sub = profile["sub"]
    email = profile.get("email")
    email_verified = bool(profile.get("email_verified"))
    user = _find_google_user(
        db,
        google_sub,
        email,
        email_verified=email_verified,
        hosted_domain=profile.get("hosted_domain"),
    )

    if user is not None:
        _apply_google_profile_to_user(user, profile)
        db.commit()
        raise _google_error(
            "Google account is already registered",
            status_code=409,
            code="GOOGLE_ACCOUNT_ALREADY_REGISTERED",
        )

    user = _create_google_user(db, body, profile)
    db.commit()
    db.refresh(user)
    return _issue_tokens(db, user)


@router.post("/google", response_model=AuthTokensDto)
def google_login_legacy(body: GoogleAuthRequest, db: Session = Depends(get_db)):
    """Backward-compatible strict Google login endpoint."""

    return google_login(body, db)


@router.post("/google/dev-register", response_model=AuthTokensDto)
def google_dev_register(body: GoogleDevAuthRequest, db: Session = Depends(get_db)):
    """Temporary local-dev fallback for Google registration.

    This exists only so mobile QA can continue while the real Google Web
    OAuth client ID is missing. It does not verify a Google identity and must
    stay disabled outside local development.
    """

    if not settings.google_dev_auth_fallback:
        raise _google_not_configured()

    nickname = (body.nickname.strip() if body.nickname else None) or "Google dev user"
    city = _valid_optional_city(body.city)
    seed = secrets.token_hex(8)
    email = f"google-dev-{seed}@local.test"
    user = User(
        nickname=nickname[:50],
        phone=None,
        email=email,
        password_hash=hash_password(secrets.token_urlsafe(32)),
        heishi_id=generate_heishi_id(db, seed),
        city=city,
        avatar_url=None,
        phone_verified=False,
        email_verified=True,
        google_sub=f"dev-google-{seed}",
    )
    db.add(user)
    db.flush()
    get_or_create_settings(db, user.id)
    issue_welcome_coupon(db, user.id, user.language)
    db.commit()
    db.refresh(user)
    return _issue_tokens(db, user)


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


def _twilio_http_error(exc: TwilioOtpError) -> HTTPException:
    if exc.code == "INVALID_PHONE":
        return HTTPException(
            status_code=422,
            detail={"code": "VALIDATION_ERROR", "message": str(exc), "details": {}},
        )
    if exc.code == "OTP_INVALID":
        return HTTPException(
            status_code=400,
            detail={"code": "OTP_INVALID", "message": "Invalid verification code", "details": {}},
        )
    if exc.code in {"TWILIO_NOT_CONFIGURED", "TWILIO_NOT_INSTALLED"}:
        return HTTPException(
            status_code=503,
            detail={"code": "TWILIO_NOT_CONFIGURED", "message": str(exc), "details": {}},
        )
    return HTTPException(
        status_code=503,
        detail={"code": "TWILIO_SEND_FAILED", "message": str(exc), "details": {}},
    )


def _should_use_twilio_verify() -> bool:
    return settings.twilio_verify_enabled and not settings.sms_dev_otp


def _should_block_partial_twilio_config() -> bool:
    return settings.twilio_verify_partially_configured and not settings.sms_dev_otp


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

    if _should_block_partial_twilio_config():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "TWILIO_NOT_CONFIGURED",
                "message": "Twilio Verify env vars must all be set together",
                "details": {},
            },
        )
    if _should_use_twilio_verify():
        try:
            send_sms_verification(phone)
        except TwilioOtpError as exc:
            raise _twilio_http_error(exc) from exc
        return SendRegisterCodeResponse(expiresIn=OTP_TTL_SECONDS, resendAfter=RESEND_COOLDOWN_SECONDS)

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
    if _should_block_partial_twilio_config():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "TWILIO_NOT_CONFIGURED",
                "message": "Twilio Verify env vars must all be set together",
                "details": {},
            },
        )
    if _should_use_twilio_verify():
        try:
            verify_sms_code(phone, body.verificationCode.strip())
        except TwilioOtpError as exc:
            raise _twilio_http_error(exc) from exc
    else:
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


def _send_login_code(db: Session, phone: str) -> SendRegisterCodeResponse:
    user = db.query(User).filter(User.phone == phone).first()
    if not user:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "No account for this phone number", "details": {}},
        )
    if _should_block_partial_twilio_config():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "TWILIO_NOT_CONFIGURED",
                "message": "Twilio Verify env vars must all be set together",
                "details": {},
            },
        )
    if _should_use_twilio_verify():
        try:
            send_sms_verification(phone)
        except TwilioOtpError as exc:
            raise _twilio_http_error(exc) from exc
        return SendRegisterCodeResponse(expiresIn=OTP_TTL_SECONDS, resendAfter=RESEND_COOLDOWN_SECONDS)
    existing = (
        db.query(PhoneOtp)
        .filter(PhoneOtp.phone == phone, PhoneOtp.purpose == "login", PhoneOtp.consumed.is_(False))
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
    issue_login_code(db, phone, code)
    print(f"[HeyMarket OTP] login {phone} -> {code}")
    return SendRegisterCodeResponse(
        expiresIn=OTP_TTL_SECONDS,
        resendAfter=RESEND_COOLDOWN_SECONDS,
        devCode=code if settings.expose_dev_otp else None,
    )


@router.post("/login/send-code", response_model=SendRegisterCodeResponse)
def send_login_code(body: SendRegisterCodeRequest, db: Session = Depends(get_db)):
    phone = _require_valid_phone(body.phone)
    return _send_login_code(db, phone)


@router.post("/login/verify", response_model=AuthTokensDto)
def login_verify(body: LoginOtpRequest, db: Session = Depends(get_db)):
    phone = _require_valid_phone(body.phone)
    if _should_block_partial_twilio_config():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "TWILIO_NOT_CONFIGURED",
                "message": "Twilio Verify env vars must all be set together",
                "details": {},
            },
        )
    if _should_use_twilio_verify():
        try:
            verify_sms_code(phone, body.verificationCode.strip())
        except TwilioOtpError as exc:
            raise _twilio_http_error(exc) from exc
    else:
        try:
            consume_login_code(db, phone, body.verificationCode.strip())
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
    user = db.query(User).filter(User.phone == phone).first()
    if not user:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "No account for this phone number", "details": {}},
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
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    record_daily_active_user(db, user.id)
    return user_to_dto(user)
