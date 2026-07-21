from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import json
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth import (
    create_access_token,
    create_refresh_token,
    generate_heishi_id,
    get_current_user,
    hash_refresh_token,
    hash_password,
    is_valid_au_phone,
    normalize_phone,
    revoke_user_refresh_tokens,
    store_refresh_token,
    validate_refresh_token,
    verify_password,
)
from app.analytics import record_daily_active_user
from app.account_merge import account_has_cross_domain_state
from app.alipay_payout_service import AlipayPayoutError, exchange_authorization_code
from app.catalog_helpers import get_or_create_settings
from app.config import settings
from app.coupon_service import issue_welcome_coupon
from app.database import get_db
from app.notification_jobs import enqueue_notification
from app.models import (
    AuthIdentity,
    Conversation,
    DeviceSession,
    Listing,
    LoginAuditLog,
    Order,
    PhoneOtp,
    RefreshToken,
    User,
)
from app.phone_verification import (
    OTP_TTL_SECONDS,
    RESEND_COOLDOWN_SECONDS,
    consume_login_code,
    consume_register_code,
    generate_code,
    issue_login_code,
    issue_otp_code,
    issue_register_code,
    resend_allowed_at,
    consume_otp_code,
)
from app.twilio_otp import TwilioOtpError, send_sms_verification, verify_sms_code
from app.schemas import (
    AuthTokensDto,
    AuthUserDto,
    AlipayAuthRequest,
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
    BindPhoneRequest,
    VerifyBindPhoneRequest,
    MergePhoneAccountRequest,
    MergeThirdPartyAccountRequest,
)
from app.serializers import user_to_dto
from app.supabase_auth import (
    avatar_from_claims,
    decode_supabase_jwt,
    email_from_claims,
    name_from_claims,
    phone_from_claims,
)


def _issue_alipay_oauth_state() -> str:
    issued_at = int(datetime.now(timezone.utc).timestamp())
    payload = f"{issued_at}.{secrets.token_urlsafe(18)}"
    signature = hmac.new(
        settings.jwt_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{payload}.{encoded_signature}"


def _validate_alipay_oauth_state(state: str | None) -> None:
    # Native Alipay SDK callbacks are protected by the SDK/application
    # signature and do not carry browser state. Browser callbacks issued by
    # /auth/alipay/authorize always include and validate this state.
    if state is None:
        return
    try:
        issued_raw, nonce, signature = state.split(".", 2)
        issued_at = int(issued_raw)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ALIPAY_OAUTH_STATE_INVALID",
                "message": "Alipay authorization state is invalid",
                "details": {},
            },
        )
    payload = f"{issued_raw}.{nonce}"
    expected = base64.urlsafe_b64encode(
        hmac.new(
            settings.jwt_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("ascii").rstrip("=")
    age_seconds = int(datetime.now(timezone.utc).timestamp()) - issued_at
    if not hmac.compare_digest(signature, expected) or not 0 <= age_seconds <= 600:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ALIPAY_OAUTH_STATE_INVALID",
                "message": "Alipay authorization state is invalid or expired",
                "details": {},
            },
        )
from app.routers.region_safety import REGION_DATA

router = APIRouter(prefix="/auth", tags=["auth"])
supabase_security = HTTPBearer(auto_error=False)

KNOWN_CITY_NAMES = {city.name for region in REGION_DATA for city in region.cities}
WECHAT_ACCESS_TOKEN_URL = "https://api.weixin.qq.com/sns/oauth2/access_token"
WECHAT_USERINFO_URL = "https://api.weixin.qq.com/sns/userinfo"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


def _record_login_failure(
    db: Session,
    request: Request,
    *,
    provider: str,
    failure_code: str,
    subject_hint: str | None = None,
    device_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """Persist a provider-neutral failed authentication audit event."""
    db.add(
        LoginAuditLog(
            user_id=user_id,
            provider=provider,
            subject_hint=subject_hint,
            event_type="login_failure",
            success=False,
            failure_code=failure_code,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            device_id=device_id,
        )
    )
    db.commit()


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
def wechat_login(body: WeChatAuthRequest, request: Request, db: Session = Depends(get_db)):
    """Sign in or register via native WeChat Open Platform authorization code.

    The mobile app obtains a one-time WeChat ``code`` from the native SDK. The
    backend exchanges that code with WeChat, stores openid/unionid, and issues
    the same HeyMarket JWT session used by phone registration/login.
    """

    try:
        profile = _wechat_exchange_code(body.code)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        _record_login_failure(
            db,
            request,
            provider="wechat",
            failure_code=str(detail.get("code") or "WECHAT_AUTH_FAILED"),
            subject_hint="wechat_authorization",
            device_id=body.deviceId,
        )
        raise
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
        return _issue_tokens(
            db,
            user,
            request=request,
            device_id=body.deviceId,
            platform=body.platform,
            device_name=body.deviceName,
            auth_provider="wechat",
        )

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
    return _issue_tokens(
        db,
        user,
        request=request,
        device_id=body.deviceId,
        platform=body.platform,
        device_name=body.deviceName,
        auth_provider="wechat",
    )


@router.get("/alipay/authorize")
def alipay_authorize():
    """Return a short-lived, CSRF-protected Alipay user-authorization URL."""
    app_id = settings.alipay_app_id.strip()
    redirect_uri = settings.alipay_oauth_redirect_url.strip()
    if not app_id or not redirect_uri:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "ALIPAY_AUTH_NOT_CONFIGURED",
                "message": "Alipay authorization is not configured",
                "details": {},
            },
        )
    state = _issue_alipay_oauth_state()
    query = urlencode(
        {
            "app_id": app_id,
            "scope": "auth_user",
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return {
        "authorizationUrl": (
            "https://openauth.alipay.com/oauth2/publicAppAuthorize.htm?"
            f"{query}"
        ),
        "state": state,
        "redirectUri": redirect_uri,
        "expiresIn": 600,
    }


@router.post("/alipay", response_model=AuthTokensDto)
def alipay_login(
    body: AlipayAuthRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Sign in or register using a server-verified Alipay OAuth code."""
    try:
        _validate_alipay_oauth_state(body.oauthState)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        _record_login_failure(
            db,
            request,
            provider="alipay",
            failure_code=str(detail.get("code") or "ALIPAY_OAUTH_STATE_INVALID"),
            subject_hint="alipay_authorization",
            device_id=body.deviceId,
        )
        raise
    try:
        profile = exchange_authorization_code(body.authCode)
    except AlipayPayoutError as exc:
        _record_login_failure(
            db,
            request,
            provider="alipay",
            failure_code="ALIPAY_AUTH_FAILED",
            subject_hint="alipay_authorization",
            device_id=body.deviceId,
        )
        raise HTTPException(
            status_code=502,
            detail={
                "code": "ALIPAY_AUTH_FAILED",
                "message": str(exc),
                "details": {},
            },
        ) from exc
    subject = str(profile.get("user_id") or profile.get("open_id") or "").strip()
    if not subject:
        _record_login_failure(
            db,
            request,
            provider="alipay",
            failure_code="ALIPAY_IDENTITY_MISSING",
            subject_hint="alipay_authorization",
            device_id=body.deviceId,
        )
        raise HTTPException(
            status_code=502,
            detail={
                "code": "ALIPAY_IDENTITY_MISSING",
                "message": "Alipay did not return a user identity",
                "details": {},
            },
        )
    identity = (
        db.query(AuthIdentity)
        .filter(
            AuthIdentity.provider == "alipay",
            AuthIdentity.provider_subject == subject,
        )
        .first()
    )
    if identity:
        user = db.query(User).filter(User.id == identity.user_id).first()
        if not user:
            _record_login_failure(
                db,
                request,
                provider="alipay",
                failure_code="AUTH_IDENTITY_ORPHANED",
                subject_hint=subject,
                device_id=body.deviceId,
                user_id=identity.user_id,
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "AUTH_IDENTITY_ORPHANED",
                    "message": "The Alipay identity is not linked to a valid account",
                    "details": {},
                },
            )
        identity.verified = True
        identity.last_used_at = datetime.now(timezone.utc)
        user.alipay_bound = True
        db.commit()
        return _issue_tokens(
            db,
            user,
            request=request,
            device_id=body.deviceId,
            platform=body.platform,
            device_name=body.deviceName,
            auth_provider="alipay",
        )

    nickname = (
        (body.nickname.strip() if body.nickname else None)
        or str(profile.get("nick_name") or profile.get("nick") or "").strip()
        or "Alipay user"
    )
    user = User(
        nickname=nickname[:50],
        phone=None,
        email=None,
        password_hash=hash_password(secrets.token_urlsafe(32)),
        heishi_id=generate_heishi_id(db, subject),
        city=_valid_optional_city(body.city),
        phone_verified=False,
        alipay_bound=True,
    )
    db.add(user)
    db.flush()
    db.add(
        AuthIdentity(
            user_id=user.id,
            provider="alipay",
            provider_subject=subject,
            verified=True,
            metadata_json=json.dumps(
                {
                    "userId": profile.get("user_id"),
                    "openId": profile.get("open_id"),
                }
            ),
            last_used_at=datetime.now(timezone.utc),
        )
    )
    get_or_create_settings(db, user.id)
    issue_welcome_coupon(db, user.id, user.language)
    db.commit()
    db.refresh(user)
    return _issue_tokens(
        db,
        user,
        request=request,
        device_id=body.deviceId,
        platform=body.platform,
        device_name=body.deviceName,
        auth_provider="alipay",
    )


@router.post("/google/login", response_model=AuthTokensDto)
def google_login(body: GoogleAuthRequest, request: Request, db: Session = Depends(get_db)):
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
    return _issue_tokens(
        db,
        user,
        request=request,
        device_id=body.deviceId,
        platform=body.platform,
        device_name=body.deviceName,
        auth_provider="google",
    )


@router.post("/google/register", response_model=AuthTokensDto)
def google_register(body: GoogleAuthRequest, request: Request, db: Session = Depends(get_db)):
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
    return _issue_tokens(
        db,
        user,
        request=request,
        device_id=body.deviceId,
        platform=body.platform,
        device_name=body.deviceName,
        auth_provider="google",
    )


@router.post("/google", response_model=AuthTokensDto)
def google_login_legacy(body: GoogleAuthRequest, request: Request, db: Session = Depends(get_db)):
    """Backward-compatible strict Google login endpoint."""

    return google_login(body, request, db)


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
    return _issue_tokens(db, user, auth_provider="google_dev")


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


def _sync_auth_identities(db: Session, user: User) -> None:
    """Backfill normalized identities while legacy columns remain compatible."""
    candidates: list[tuple[str, str, bool, dict]] = []
    if user.phone:
        candidates.append(("phone", user.phone, bool(user.phone_verified), {}))
    if user.wechat_unionid or user.wechat_openid:
        candidates.append(
            (
                "wechat",
                user.wechat_unionid or user.wechat_openid,
                bool(user.wechat_bound),
                {"openid": user.wechat_openid, "unionid": user.wechat_unionid},
            )
        )
    if user.google_sub:
        candidates.append(("google", user.google_sub, bool(user.email_verified), {"email": user.email}))
    for provider, subject, verified, metadata in candidates:
        existing = (
            db.query(AuthIdentity)
            .filter(
                AuthIdentity.provider == provider,
                AuthIdentity.provider_subject == subject,
            )
            .first()
        )
        if existing and existing.user_id != user.id:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "IDENTITY_CONFLICT",
                    "message": f"This {provider} identity is already bound to another account",
                    "details": {},
                },
            )
        if not existing:
            db.add(
                AuthIdentity(
                    user_id=user.id,
                    provider=provider,
                    provider_subject=subject,
                    verified=verified,
                    metadata_json=json.dumps(metadata),
                    last_used_at=datetime.now(timezone.utc),
                )
            )
        else:
            existing.verified = existing.verified or verified
            existing.metadata_json = json.dumps(metadata)
            existing.last_used_at = datetime.now(timezone.utc)


def _apply_identity_to_legacy_user(user: User, identity: AuthIdentity) -> None:
    """Keep legacy login columns synchronized during the normalized migration."""
    try:
        metadata = json.loads(identity.metadata_json or "{}")
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    if identity.provider == "phone":
        user.phone = identity.provider_subject
        user.phone_verified = identity.verified
    elif identity.provider == "wechat":
        user.wechat_openid = metadata.get("openid") or identity.provider_subject
        user.wechat_unionid = metadata.get("unionid")
        user.wechat_bound = identity.verified
    elif identity.provider == "alipay":
        user.alipay_bound = identity.verified
    elif identity.provider == "google":
        user.google_sub = identity.provider_subject


def _clear_legacy_identities(user: User) -> None:
    user.phone = None
    user.phone_verified = False
    user.wechat_openid = None
    user.wechat_unionid = None
    user.wechat_bound = False
    user.alipay_bound = False
    user.google_sub = None


def _issue_tokens(
    db: Session,
    user: User,
    *,
    request: Request | None = None,
    device_id: str | None = None,
    platform: str | None = None,
    device_name: str | None = None,
    suspicious: bool = False,
    existing_session: DeviceSession | None = None,
    auth_provider: str = "session",
) -> AuthTokensDto:
    if user.account_status != "normal":
        if request is not None:
            _record_login_failure(
                db,
                request,
                provider=auth_provider,
                failure_code="ACCOUNT_SUSPENDED",
                subject_hint=user.phone[-4:] if user.phone else user.heishi_id,
                device_id=device_id,
                user_id=user.id,
            )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ACCOUNT_SUSPENDED",
                "message": "This account is not permitted to sign in",
                "details": {"accountStatus": user.account_status},
            },
        )
    _sync_auth_identities(db, user)
    if device_id and existing_session is None:
        existing_session = (
            db.query(DeviceSession)
            .filter(
                DeviceSession.user_id == user.id,
                DeviceSession.device_id == device_id,
                DeviceSession.revoked_at.is_(None),
            )
            .order_by(DeviceSession.last_seen_at.desc())
            .first()
        )
        if existing_session is None:
            suspicious = suspicious or (
                db.query(DeviceSession.id)
                .filter(DeviceSession.user_id == user.id)
                .first()
                is not None
            )
    refresh = create_refresh_token()
    refresh_record = store_refresh_token(db, user.id, refresh)
    if existing_session:
        if existing_session.refresh_token_id:
            previous_refresh = (
                db.query(RefreshToken)
                .filter(RefreshToken.id == existing_session.refresh_token_id)
                .first()
            )
            if previous_refresh:
                previous_refresh.revoked = True
        existing_session.refresh_token_id = refresh_record.id
        existing_session.last_seen_at = datetime.now(timezone.utc)
        existing_session.ip_address = (
            request.client.host if request and request.client else existing_session.ip_address
        )
        existing_session.user_agent = (
            request.headers.get("user-agent") if request else existing_session.user_agent
        )
        existing_session.platform = (platform or existing_session.platform or "unknown").lower()
        existing_session.device_name = device_name or existing_session.device_name
        existing_session.suspicious = existing_session.suspicious or suspicious
        existing_session.revoked_at = None
    else:
        session = DeviceSession(
            user_id=user.id,
            refresh_token_id=refresh_record.id,
            device_id=device_id or f"legacy-{secrets.token_urlsafe(18)}",
            platform=(platform or "unknown").lower(),
            device_name=device_name,
            ip_address=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
            suspicious=suspicious,
        )
        db.add(session)
        existing_session = session
    db.flush()
    access = create_access_token(user.id, existing_session.id)
    db.add(
        LoginAuditLog(
            user_id=user.id,
            provider=auth_provider,
            subject_hint=user.phone[-4:] if user.phone else user.heishi_id,
            event_type="login_success",
            success=True,
            ip_address=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
            device_id=device_id,
        )
    )
    if suspicious:
        enqueue_notification(
            db,
            user_id=user.id,
            role="buyer",
            category="account_security",
            notification_type="suspicious_login",
            title="New or suspicious sign-in",
            body="A sign-in that may be unusual was detected. Review your active sessions.",
            title_zh="检测到新的或可疑的登录",
            body_zh="检测到可能异常的登录，请检查您的活跃设备会话。",
            business_type="account",
            business_id=user.id,
            deep_link="heymarket://settings/account-safety",
            deduplication_key=f"security:login:{refresh_record.id}",
            mandatory=True,
        )
    db.commit()
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
    if settings.expose_dev_otp:
        print(f"[HeyMarket OTP] register {phone} -> {code}")

    return SendRegisterCodeResponse(
        expiresIn=OTP_TTL_SECONDS,
        resendAfter=RESEND_COOLDOWN_SECONDS,
        devCode=code if settings.expose_dev_otp else None,
    )


@router.post("/register", response_model=AuthTokensDto, status_code=201)
def register(
    body: RegisterRequest,
    request: Request,
    db: Session = Depends(get_db),
):
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
    return _issue_tokens(
        db,
        user,
        request=request,
        device_id=body.deviceId,
        platform=body.platform,
        device_name=body.deviceName,
        auth_provider="phone_register",
    )


@router.post("/login", response_model=AuthTokensDto)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    phone = _require_valid_phone(body.phone)
    user = db.query(User).filter(User.phone == phone).first()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
    client_ip = request.client.host if request.client else None
    if client_ip:
        ip_failures = (
            db.query(LoginAuditLog)
            .filter(
                LoginAuditLog.ip_address == client_ip,
                LoginAuditLog.success.is_(False),
                LoginAuditLog.created_at >= cutoff,
            )
            .count()
        )
        if ip_failures >= 20:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "LOGIN_IP_RATE_LIMIT",
                    "message": "Too many failed login attempts from this network. Try again later.",
                    "details": {"retryAfter": 900},
                },
            )
    recent_failures = (
        db.query(LoginAuditLog)
        .filter(
            LoginAuditLog.subject_hint == phone,
            LoginAuditLog.success.is_(False),
            LoginAuditLog.created_at >= cutoff,
        )
        .count()
    )
    if recent_failures >= 5:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "LOGIN_RATE_LIMIT",
                "message": "Too many failed login attempts. Try again later.",
                "details": {"retryAfter": 900},
            },
        )
    if not user or not verify_password(body.password, user.password_hash):
        db.add(
            LoginAuditLog(
                user_id=user.id if user else None,
                provider="phone",
                subject_hint=phone,
                event_type="login_failure",
                success=False,
                failure_code="INVALID_CREDENTIALS",
                ip_address=client_ip,
                user_agent=request.headers.get("user-agent"),
                device_id=body.deviceId,
            )
        )
        db.commit()
        raise HTTPException(
            status_code=401,
            detail={"code": "INVALID_CREDENTIALS", "message": "Invalid phone or password", "details": {}},
        )
    if user.account_status != "normal":
        _record_login_failure(
            db,
            request,
            provider="phone_password",
            failure_code="ACCOUNT_SUSPENDED",
            subject_hint=phone,
            device_id=body.deviceId,
            user_id=user.id,
        )
        raise HTTPException(
            status_code=403,
            detail={"code": "ACCOUNT_SUSPENDED", "message": "This account is suspended", "details": {}},
        )
    known_session: DeviceSession | None = None
    has_prior_sessions = False
    if body.deviceId:
        known_session = (
            db.query(DeviceSession)
            .filter(
                DeviceSession.user_id == user.id,
                DeviceSession.device_id == body.deviceId,
                DeviceSession.revoked_at.is_(None),
            )
            .order_by(DeviceSession.last_seen_at.desc())
            .first()
        )
        has_prior_sessions = (
            db.query(DeviceSession.id)
            .filter(DeviceSession.user_id == user.id)
            .first()
            is not None
        )
    suspicious = recent_failures >= 2 or (
        bool(body.deviceId) and has_prior_sessions and known_session is None
    )
    return _issue_tokens(
        db,
        user,
        request=request,
        device_id=body.deviceId,
        platform=body.platform,
        device_name=body.deviceName,
        suspicious=suspicious,
        existing_session=known_session,
        auth_provider="phone_password",
    )


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
    if settings.expose_dev_otp:
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
def login_verify(body: LoginOtpRequest, request: Request, db: Session = Depends(get_db)):
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
            _record_login_failure(
                db,
                request,
                provider="phone",
                failure_code="OTP_VERIFICATION_FAILED",
                subject_hint=phone,
                device_id=body.deviceId,
            )
            raise _twilio_http_error(exc) from exc
    else:
        try:
            consume_login_code(db, phone, body.verificationCode.strip())
        except ValueError as exc:
            reason = str(exc)
            _record_login_failure(
                db,
                request,
                provider="phone",
                failure_code=reason,
                subject_hint=phone,
                device_id=body.deviceId,
            )
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
        _record_login_failure(
            db,
            request,
            provider="phone",
            failure_code="ACCOUNT_NOT_FOUND",
            subject_hint=phone,
            device_id=body.deviceId,
        )
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "No account for this phone number", "details": {}},
        )
    if user.account_status != "normal":
        _record_login_failure(
            db,
            request,
            provider="phone",
            failure_code="ACCOUNT_SUSPENDED",
            subject_hint=phone,
            device_id=body.deviceId,
            user_id=user.id,
        )
        raise HTTPException(
            status_code=403,
            detail={"code": "ACCOUNT_SUSPENDED", "message": "This account is suspended", "details": {}},
        )
    existing_session = None
    has_prior_sessions = False
    if body.deviceId:
        existing_session = (
            db.query(DeviceSession)
            .filter(
                DeviceSession.user_id == user.id,
                DeviceSession.device_id == body.deviceId,
                DeviceSession.revoked_at.is_(None),
            )
            .order_by(DeviceSession.last_seen_at.desc())
            .first()
        )
        has_prior_sessions = (
            db.query(DeviceSession.id)
            .filter(DeviceSession.user_id == user.id)
            .first()
            is not None
        )
    return _issue_tokens(
        db,
        user,
        request=request,
        device_id=body.deviceId,
        platform=body.platform,
        device_name=body.deviceName,
        suspicious=bool(body.deviceId) and has_prior_sessions and existing_session is None,
        existing_session=existing_session,
        auth_provider="phone_otp",
    )


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
def refresh(body: RefreshRequest, request: Request, db: Session = Depends(get_db)):
    old_record = (
        db.query(RefreshToken)
        .filter(
            RefreshToken.token_hash == hash_refresh_token(body.refreshToken),
            RefreshToken.revoked.is_(False),
        )
        .with_for_update()
        .first()
    )
    user = validate_refresh_token(db, body.refreshToken)
    if not user or not old_record:
        raise HTTPException(
            status_code=401,
            detail={"code": "TOKEN_EXPIRED", "message": "Invalid or expired refresh token", "details": {}},
        )
    existing_session = (
        db.query(DeviceSession)
        .filter(
            DeviceSession.refresh_token_id == old_record.id,
            DeviceSession.user_id == user.id,
            DeviceSession.revoked_at.is_(None),
        )
        .first()
    )
    # Rotate, never clone. A stolen refresh token cannot be replayed after use,
    # and the logical device remains one session in the user's security view.
    old_record.revoked = True
    db.commit()
    return _issue_tokens(
        db,
        user,
        request=request,
        existing_session=existing_session,
        auth_provider="refresh",
    )


@router.get("/me", response_model=AuthUserDto)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    record_daily_active_user(db, user.id)
    return user_to_dto(user)


@router.get("/identities")
def list_auth_identities(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _sync_auth_identities(db, user)
    db.commit()
    rows = (
        db.query(AuthIdentity)
        .filter(AuthIdentity.user_id == user.id)
        .order_by(AuthIdentity.bound_at.asc())
        .all()
    )
    return [
        {
            "id": row.id,
            "provider": row.provider,
            "verified": row.verified,
            "boundAt": row.bound_at.isoformat(),
            "lastUsedAt": row.last_used_at.isoformat() if row.last_used_at else None,
        }
        for row in rows
    ]


@router.post("/identities/phone/send-code", response_model=SendRegisterCodeResponse)
def send_bind_phone_code(
    body: BindPhoneRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    phone = _require_valid_phone(body.phone)
    existing = (
        db.query(PhoneOtp)
        .filter(
            PhoneOtp.phone == phone,
            PhoneOtp.purpose == "bind_phone",
            PhoneOtp.consumed.is_(False),
        )
        .first()
    )
    if existing is not None:
        allowed_at = resend_allowed_at(existing)
        now = datetime.now(timezone.utc)
        if now < allowed_at:
            wait = max(1, int((allowed_at - now).total_seconds()))
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "OTP_RATE_LIMIT",
                    "message": f"Please wait {wait}s before requesting another code",
                    "details": {"retryAfter": wait},
                },
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
        return SendRegisterCodeResponse(
            expiresIn=OTP_TTL_SECONDS,
            resendAfter=RESEND_COOLDOWN_SECONDS,
        )
    code = generate_code()
    issue_otp_code(db, phone, "bind_phone", code)
    if settings.expose_dev_otp:
        print(f"[HeyMarket OTP] bind_phone {phone} -> {code}")
    return SendRegisterCodeResponse(
        expiresIn=OTP_TTL_SECONDS,
        resendAfter=RESEND_COOLDOWN_SECONDS,
        devCode=code if settings.expose_dev_otp else None,
    )


@router.post("/identities/phone/verify")
def verify_and_bind_phone(
    body: VerifyBindPhoneRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    phone = _require_valid_phone(body.phone)
    if _should_use_twilio_verify():
        try:
            verify_sms_code(phone, body.verificationCode)
        except TwilioOtpError as exc:
            raise _twilio_http_error(exc) from exc
    else:
        try:
            consume_otp_code(db, phone, "bind_phone", body.verificationCode)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": str(exc),
                    "message": "Invalid or expired verification code",
                    "details": {},
                },
            ) from exc
    owner = db.query(User).filter(User.phone == phone, User.id != user.id).first()
    if owner:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ACCOUNT_MERGE_REQUIRED",
                "message": "This phone belongs to another account. Authorize an account merge to continue.",
                "details": {"provider": "phone"},
            },
        )
    existing_phone_identity = (
        db.query(AuthIdentity)
        .filter(
            AuthIdentity.user_id == user.id,
            AuthIdentity.provider == "phone",
        )
        .first()
    )
    if existing_phone_identity and existing_phone_identity.provider_subject != phone:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PROVIDER_ALREADY_BOUND",
                "message": "A phone number is already bound to this account",
                "details": {},
            },
        )
    user.phone = phone
    user.phone_verified = True
    _sync_auth_identities(db, user)
    db.commit()
    return {"bound": True, "provider": "phone"}


@router.post("/identities/wechat/bind")
def bind_wechat_identity(
    body: WeChatAuthRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = _wechat_exchange_code(body.code)
    openid = str(profile["openid"])
    unionid = str(profile.get("unionid") or "") or None
    subject = unionid or openid
    existing = (
        db.query(AuthIdentity)
        .filter(
            AuthIdentity.provider == "wechat",
            AuthIdentity.provider_subject == subject,
        )
        .first()
    )
    legacy_owner = _find_wechat_user(db, openid, unionid)
    owner_id = existing.user_id if existing else legacy_owner.id if legacy_owner else None
    if owner_id and owner_id != user.id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ACCOUNT_MERGE_REQUIRED",
                "message": "This WeChat identity belongs to another account",
                "details": {"provider": "wechat"},
            },
        )
    user.wechat_openid = openid
    user.wechat_unionid = unionid
    user.wechat_bound = True
    _sync_auth_identities(db, user)
    db.commit()
    return {"bound": True, "provider": "wechat"}


@router.post("/identities/google/bind")
def bind_google_identity(
    body: GoogleAuthRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = _google_exchange_id_token(body.idToken)
    subject = str(profile["sub"])
    existing = (
        db.query(AuthIdentity)
        .filter(
            AuthIdentity.provider == "google",
            AuthIdentity.provider_subject == subject,
        )
        .first()
    )
    legacy_owner = db.query(User).filter(User.google_sub == subject).first()
    owner_id = existing.user_id if existing else legacy_owner.id if legacy_owner else None
    if owner_id and owner_id != user.id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ACCOUNT_MERGE_REQUIRED",
                "message": "This Google identity belongs to another account",
                "details": {"provider": "google"},
            },
        )
    _apply_google_profile_to_user(user, profile)
    _sync_auth_identities(db, user)
    db.commit()
    return {"bound": True, "provider": "google"}


@router.post("/identities/alipay/bind")
def bind_alipay_identity(
    body: AlipayAuthRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _validate_alipay_oauth_state(body.oauthState)
    try:
        profile = exchange_authorization_code(body.authCode)
    except AlipayPayoutError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "ALIPAY_AUTH_FAILED",
                "message": str(exc),
                "details": {},
            },
        ) from exc
    subject = str(profile.get("user_id") or profile.get("open_id") or "").strip()
    if not subject:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "ALIPAY_IDENTITY_MISSING",
                "message": "Alipay did not return a user identity",
                "details": {},
            },
        )
    owner = (
        db.query(AuthIdentity)
        .filter(
            AuthIdentity.provider == "alipay",
            AuthIdentity.provider_subject == subject,
        )
        .first()
    )
    if owner and owner.user_id != user.id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ACCOUNT_MERGE_REQUIRED",
                "message": "This Alipay identity belongs to another account",
                "details": {"provider": "alipay"},
            },
        )
    current = (
        db.query(AuthIdentity)
        .filter(
            AuthIdentity.user_id == user.id,
            AuthIdentity.provider == "alipay",
        )
        .first()
    )
    if current and current.provider_subject != subject:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PROVIDER_ALREADY_BOUND",
                "message": "An Alipay identity is already bound to this account",
                "details": {},
            },
        )
    now = datetime.now(timezone.utc)
    if not current:
        current = AuthIdentity(
            user_id=user.id,
            provider="alipay",
            provider_subject=subject,
            verified=True,
            metadata_json=json.dumps(
                {
                    "userId": profile.get("user_id"),
                    "openId": profile.get("open_id"),
                }
            ),
            last_used_at=now,
        )
        db.add(current)
    else:
        current.verified = True
        current.last_used_at = now
    user.alipay_bound = True
    db.commit()
    return {"bound": True, "provider": "alipay"}


@router.post("/account-merge/phone", response_model=AuthTokensDto)
def merge_phone_account(
    body: MergePhoneAccountRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Merge an empty duplicate phone account after password proof.

    Accounts with marketplace history require an administrator-assisted merge so
    ownership, settlement, audit, and dispute records are never silently rewritten.
    """
    phone = _require_valid_phone(body.phone)
    target = db.query(User).filter(User.phone == phone).first()
    if not target or not verify_password(body.password, target.password_hash):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "MERGE_AUTHORIZATION_FAILED",
                "message": "The phone account credentials could not be verified",
                "details": {},
            },
        )
    if target.id == user.id:
        return _issue_tokens(db, user, request=request, auth_provider="phone_merge")
    if target.is_admin or user.is_admin:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MERGE_NOT_ALLOWED",
                "message": "Administrator accounts cannot be merged",
                "details": {},
            },
        )
    if target.account_status != "normal":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MERGE_ACCOUNT_RESTRICTED",
                "message": "A restricted account cannot be merged into another account",
                "details": {"targetStatus": target.account_status},
            },
        )
    has_marketplace_history = any(
        (
            db.query(Listing.id).filter(Listing.seller_id == target.id).first(),
            db.query(Order.id)
            .filter((Order.buyer_id == target.id) | (Order.seller_id == target.id))
            .first(),
            db.query(Conversation.id)
            .filter(
                (Conversation.buyer_id == target.id)
                | (Conversation.seller_id == target.id)
            )
            .first(),
        )
    )
    if has_marketplace_history or account_has_cross_domain_state(db, target.id):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MERGE_REQUIRES_SUPPORT",
                "message": "This account has marketplace or account history and requires an administrator-assisted merge",
                "details": {},
            },
        )
    _sync_auth_identities(db, target)
    _sync_auth_identities(db, user)
    db.flush()
    existing_providers = {
        row.provider
        for row in db.query(AuthIdentity).filter(AuthIdentity.user_id == user.id).all()
    }
    for identity in db.query(AuthIdentity).filter(AuthIdentity.user_id == target.id).all():
        if identity.provider in existing_providers:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "MERGE_IDENTITY_CONFLICT",
                    "message": f"Both accounts already have a {identity.provider} login",
                    "details": {},
                },
            )
        identity.user_id = user.id
        _apply_identity_to_legacy_user(user, identity)
        existing_providers.add(identity.provider)
    _clear_legacy_identities(target)
    target.account_status = "merged"
    target.suspended_at = datetime.now(timezone.utc)
    target.admin_notes = f"Merged into user {user.id} through phone authorization."
    target.password_hash = hash_password(secrets.token_urlsafe(32))
    revoke_user_refresh_tokens(db, target.id)
    db.commit()
    return _issue_tokens(db, user, request=request, auth_provider="phone_merge")


@router.post("/account-merge/identity")
def merge_third_party_account(
    body: MergeThirdPartyAccountRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Merge an empty duplicate account after provider authorization proof."""
    if body.provider == "wechat":
        profile = _wechat_exchange_code(body.authorizationCode)
        subject = str(profile.get("unionid") or profile.get("openid") or "")
    else:
        try:
            profile = exchange_authorization_code(body.authorizationCode)
        except AlipayPayoutError as exc:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "MERGE_AUTHORIZATION_FAILED",
                    "message": str(exc),
                    "details": {"provider": body.provider},
                },
            ) from exc
        subject = str(profile.get("user_id") or profile.get("open_id") or "")
    if not subject:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "MERGE_AUTHORIZATION_FAILED",
                "message": "The provider did not return an authorized identity",
                "details": {"provider": body.provider},
            },
        )
    identity = (
        db.query(AuthIdentity)
        .filter(
            AuthIdentity.provider == body.provider,
            AuthIdentity.provider_subject == subject,
        )
        .first()
    )
    if not identity and body.provider == "wechat":
        legacy_target = _find_wechat_user(
            db,
            str(profile.get("openid") or ""),
            str(profile.get("unionid") or "") or None,
        )
        if legacy_target:
            _sync_auth_identities(db, legacy_target)
            db.flush()
            identity = (
                db.query(AuthIdentity)
                .filter(
                    AuthIdentity.provider == body.provider,
                    AuthIdentity.provider_subject == subject,
                )
                .first()
            )
    if not identity:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "MERGE_ACCOUNT_NOT_FOUND",
                "message": "No account is associated with that authorized identity",
                "details": {"provider": body.provider},
            },
        )
    target = db.query(User).filter(User.id == identity.user_id).first()
    if not target or target.id == user.id:
        return {"merged": False, "provider": body.provider}
    if target.is_admin or user.is_admin:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MERGE_NOT_ALLOWED",
                "message": "Administrator accounts cannot be merged",
                "details": {},
            },
        )
    if target.account_status != "normal":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MERGE_ACCOUNT_RESTRICTED",
                "message": "A restricted account cannot be merged into another account",
                "details": {
                    "provider": body.provider,
                    "targetStatus": target.account_status,
                },
            },
        )
    has_marketplace_history = any(
        (
            db.query(Listing.id).filter(Listing.seller_id == target.id).first(),
            db.query(Order.id)
            .filter((Order.buyer_id == target.id) | (Order.seller_id == target.id))
            .first(),
            db.query(Conversation.id)
            .filter(
                (Conversation.buyer_id == target.id)
                | (Conversation.seller_id == target.id)
            )
            .first(),
        )
    )
    if has_marketplace_history or account_has_cross_domain_state(db, target.id):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MERGE_REQUIRES_SUPPORT",
                "message": (
                    "This account has marketplace or account history and requires an "
                    "administrator-assisted merge"
                ),
                "details": {"provider": body.provider},
            },
        )
    _sync_auth_identities(db, target)
    _sync_auth_identities(db, user)
    db.flush()
    existing_by_provider = {
        row.provider: row
        for row in db.query(AuthIdentity).filter(AuthIdentity.user_id == user.id).all()
    }
    target_identities = (
        db.query(AuthIdentity).filter(AuthIdentity.user_id == target.id).all()
    )
    for target_identity in target_identities:
        if target_identity.provider in existing_by_provider:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "MERGE_IDENTITY_CONFLICT",
                    "message": (
                        f"Both accounts already have a "
                        f"{target_identity.provider} login"
                    ),
                    "details": {},
                },
            )
    for target_identity in target_identities:
        target_identity.user_id = user.id
        _apply_identity_to_legacy_user(user, target_identity)
    _clear_legacy_identities(target)
    target.account_status = "merged"
    target.suspended_at = datetime.now(timezone.utc)
    target.admin_notes = (
        f"Merged into user {user.id} through {body.provider} authorization."
    )
    target.password_hash = hash_password(secrets.token_urlsafe(32))
    revoke_user_refresh_tokens(db, target.id)
    db.commit()
    return {"merged": True, "provider": body.provider}


@router.delete("/identities/{identity_id}", status_code=204)
def unbind_auth_identity(
    identity_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = db.query(AuthIdentity).filter(AuthIdentity.user_id == user.id).all()
    target = next((row for row in rows if row.id == identity_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Authentication identity not found")
    if len([row for row in rows if row.verified]) <= 1 and target.verified:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "LAST_LOGIN_METHOD",
                "message": "Bind another verified login method before unbinding this one",
                "details": {},
            },
        )
    if target.provider == "phone":
        user.phone = None
        user.phone_verified = False
    elif target.provider == "wechat":
        user.wechat_openid = None
        user.wechat_unionid = None
        user.wechat_bound = False
    elif target.provider == "alipay":
        user.alipay_bound = False
    elif target.provider == "google":
        user.google_sub = None
    db.delete(target)
    db.commit()
    return Response(status_code=204)


@router.get("/sessions")
def list_device_sessions(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(DeviceSession)
        .filter(DeviceSession.user_id == user.id, DeviceSession.revoked_at.is_(None))
        .order_by(DeviceSession.last_seen_at.desc())
        .all()
    )
    return [
        {
            "id": row.id,
            "deviceId": row.device_id,
            "platform": row.platform,
            "deviceName": row.device_name,
            "countryCode": row.country_code,
            "suspicious": row.suspicious,
            "lastSeenAt": row.last_seen_at.isoformat(),
            "createdAt": row.created_at.isoformat(),
        }
        for row in rows
    ]


@router.delete("/sessions/{session_id}", status_code=204)
def revoke_device_session(
    session_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = (
        db.query(DeviceSession)
        .filter(DeviceSession.id == session_id, DeviceSession.user_id == user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Device session not found")
    row.revoked_at = datetime.now(timezone.utc)
    if row.refresh_token_id:
        refresh_row = db.query(RefreshToken).filter(RefreshToken.id == row.refresh_token_id).first()
        if refresh_row:
            refresh_row.revoked = True
    db.commit()
    return Response(status_code=204)
