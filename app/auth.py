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
from app.models import RefreshToken, User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ALGORITHM = "HS256"
security = HTTPBearer(auto_error=False)
AU_PHONE_RE = re.compile(r"^(\+?61|0)\d{8,10}$")


def normalize_phone(phone: str) -> str:
    return re.sub(r"\s+", "", phone.strip())


def is_valid_au_phone(phone: str) -> bool:
    return bool(AU_PHONE_RE.match(normalize_phone(phone)))


def generate_heishi_id(db: Session, phone: str) -> str:
    base = f"HS{phone[-8:]}"
    if not db.query(User).filter(User.heishi_id == base).first():
        return base
    return f"{base}{uuid.uuid4().hex[:4].upper()}"


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(seconds=settings.jwt_access_expire_seconds)
    payload = {"sub": user_id, "exp": expire, "type": "access"}
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


def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
) -> User | None:
    if not credentials:
        return None
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=[ALGORITHM])
        if payload.get("type") != "access":
            return None
        user_id = payload.get("sub")
        if not user_id:
            return None
    except JWTError:
        return None
    return db.query(User).filter(User.id == user_id).first()


def get_current_user(user: User | None = Depends(get_current_user_optional)) -> User:
    if not user:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHORIZED", "message": "Authentication required", "details": {}},
        )
    return user


def get_accept_language(request: Request) -> str:
    lang = request.headers.get("Accept-Language", "en")
    return "zh" if lang.lower().startswith("zh") else "en"
