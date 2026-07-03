from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy.orm import Session

from app.models import PhoneOtp, ensure_utc, utcnow

OTP_TTL_SECONDS = 600
RESEND_COOLDOWN_SECONDS = 60
MAX_VERIFY_ATTEMPTS = 5


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().encode()).hexdigest()


def generate_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def issue_otp_code(db: Session, phone: str, purpose: str, code: str) -> PhoneOtp:
    now = utcnow()
    row = (
        db.query(PhoneOtp)
        .filter(PhoneOtp.phone == phone, PhoneOtp.purpose == purpose)
        .first()
    )
    if row is None:
        row = PhoneOtp(phone=phone, purpose=purpose)
        db.add(row)
    row.code_hash = _hash_code(code)
    row.expires_at = now + timedelta(seconds=OTP_TTL_SECONDS)
    row.consumed = False
    row.attempts = 0
    row.created_at = now
    db.commit()
    db.refresh(row)
    return row


def issue_register_code(db: Session, phone: str, code: str) -> PhoneOtp:
    return issue_otp_code(db, phone, "register", code)


def issue_login_code(db: Session, phone: str, code: str) -> PhoneOtp:
    return issue_otp_code(db, phone, "login", code)


def resend_allowed_at(row: PhoneOtp) -> datetime:
    return ensure_utc(row.created_at) + timedelta(seconds=RESEND_COOLDOWN_SECONDS)


def consume_otp_code(db: Session, phone: str, purpose: str, code: str) -> None:
    row = (
        db.query(PhoneOtp)
        .filter(PhoneOtp.phone == phone, PhoneOtp.purpose == purpose, PhoneOtp.consumed.is_(False))
        .first()
    )
    if row is None:
        raise ValueError("OTP_NOT_FOUND")
    now = utcnow()
    expires_at = ensure_utc(row.expires_at)
    if now > expires_at:
        raise ValueError("OTP_EXPIRED")
    if row.attempts >= MAX_VERIFY_ATTEMPTS:
        raise ValueError("OTP_TOO_MANY_ATTEMPTS")
    if not secrets.compare_digest(row.code_hash, _hash_code(code)):
        row.attempts += 1
        db.commit()
        raise ValueError("OTP_INVALID")
    row.consumed = True
    db.commit()


def consume_register_code(db: Session, phone: str, code: str) -> None:
    consume_otp_code(db, phone, "register", code)


def consume_login_code(db: Session, phone: str, code: str) -> None:
    consume_otp_code(db, phone, "login", code)
