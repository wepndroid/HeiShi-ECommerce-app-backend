from datetime import datetime, timedelta, timezone
import hashlib
import re
import secrets
import uuid

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import DeviceSession, RefreshToken, User
from app.supabase_auth import decode_supabase_jwt, phone_from_claims

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ALGORITHM = "HS256"
security = HTTPBearer(auto_error=False)
AU_PHONE_RE = re.compile(r"^\+61\d{9}$")
CN_PHONE_RE = re.compile(r"^\+861[3-9]\d{9}$")
GLOBAL_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def normalize_phone(phone: str) -> str:
    cleaned = re.sub(r"[\s().-]+", "", phone.strip())
    if cleaned.startswith("+86"):
        digits = cleaned[3:]
        if re.fullmatch(r"1[3-9]\d{9}", digits):
            return f"+86{digits}"
        return cleaned
    if cleaned.startswith("86") and len(cleaned) >= 13:
        digits = cleaned[2:]
        if re.fullmatch(r"1[3-9]\d{9}", digits):
            return f"+86{digits}"
    if re.fullmatch(r"1[3-9]\d{9}", cleaned):
        return f"+86{cleaned}"
    if re.fullmatch(r"\+61\d{9}", cleaned):
        return cleaned
    if re.fullmatch(r"61\d{9}", cleaned):
        return f"+{cleaned}"
    if re.fullmatch(r"0\d{9}", cleaned):
        return f"+61{cleaned[1:]}"
    return cleaned


def is_valid_phone(phone: str) -> bool:
    normalized = normalize_phone(phone)
    if normalized.startswith("+86"):
        return bool(CN_PHONE_RE.match(normalized))
    return bool(AU_PHONE_RE.match(normalized) or GLOBAL_E164_RE.match(normalized))


def is_valid_au_phone(phone: str) -> bool:
    return is_valid_phone(phone)


def generate_heishi_id(db: Session, phone: str) -> str:
    base = f"HS{phone[-8:]}"
    if not db.query(User).filter(User.heishi_id == base).first():
        return base
    return f"{base}{uuid.uuid4().hex[:4].upper()}"


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(user_id: str, session_id: str | None = None) -> str:
    expire = datetime.now(timezone.utc) + timedelta(seconds=settings.jwt_access_expire_seconds)
    payload = {"sub": user_id, "exp": expire, "type": "access"}
    if session_id:
        payload["sid"] = session_id
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def store_refresh_token(db: Session, user_id: str, token: str) -> RefreshToken:
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.jwt_refresh_expire_days)
    record = RefreshToken(
        user_id=user_id,
        token_hash=hash_refresh_token(token),
        expires_at=expires_at,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def revoke_user_refresh_tokens(db: Session, user_id: str) -> None:
    tokens = db.query(RefreshToken).filter(RefreshToken.user_id == user_id, RefreshToken.revoked.is_(False)).all()
    for t in tokens:
        t.revoked = True
    # Session rows are the user-visible representation of those refresh
    # credentials. Keep them synchronized so logout, password changes, account
    # suspension, and merges cannot leave a revoked device shown as active.
    now = datetime.now(timezone.utc)
    sessions = (
        db.query(DeviceSession)
        .filter(DeviceSession.user_id == user_id, DeviceSession.revoked_at.is_(None))
        .all()
    )
    for session in sessions:
        session.revoked_at = now
    db.commit()


def validate_refresh_token(db: Session, token: str) -> User | None:
    record = (
        db.query(RefreshToken)
        .filter(RefreshToken.token_hash == hash_refresh_token(token), RefreshToken.revoked.is_(False))
        .first()
    )
    if not record:
        return None
    if record.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        record.revoked = True
        db.commit()
        return None
    user = db.query(User).filter(User.id == record.user_id).first()
    if not user:
        return None
    record.revoked = True
    db.commit()
    return user


def _user_from_legacy_token(token: str, db: Session) -> User | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
        if payload.get("type") != "access":
            return None
        user_id = payload.get("sub")
        if not user_id:
            return None
        session_id = payload.get("sid")
    except JWTError:
        return None
    if session_id:
        active_session = (
            db.query(DeviceSession.id)
            .filter(
                DeviceSession.id == session_id,
                DeviceSession.user_id == user_id,
                DeviceSession.revoked_at.is_(None),
            )
            .first()
        )
        if not active_session:
            return None
    return db.query(User).filter(User.id == user_id).first()


def _user_from_supabase_token(token: str, db: Session) -> User | None:
    claims = decode_supabase_jwt(token)
    if not claims:
        return None
    user_id = claims["sub"]
    return db.query(User).filter(User.id == user_id).first()


def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
) -> User | None:
    if not credentials:
        return None
    token = credentials.credentials
    if settings.supabase_auth_enabled:
        user = _user_from_supabase_token(token, db)
        if user:
            return user
    return _user_from_legacy_token(token, db)


def get_current_user(user: User | None = Depends(get_current_user_optional)) -> User:
    if not user:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "AUTHENTICATION_REQUIRED",
                "message": "Authentication is required to perform this action.",
                "details": {},
            },
        )
    if user.account_status in {"banned", "suspended", "merged"}:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ACCOUNT_SUSPENDED",
                "message": "This account is not permitted to perform authenticated actions",
                "details": {"accountStatus": user.account_status},
            },
        )
    return user


def get_accept_language(request: Request) -> str:
    lang = request.headers.get("Accept-Language", "en")
    return "zh" if lang.lower().startswith("zh") else "en"
