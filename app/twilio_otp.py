from __future__ import annotations

import re
from functools import lru_cache

from app.config import settings


class TwilioOtpError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _clean_phone(phone: str) -> str:
    return re.sub(r"\s+", "", phone.strip())


def to_e164_phone(phone: str) -> str:
    """Convert canonical or legacy AU/CN phone formats to Twilio E.164."""
    cleaned = _clean_phone(phone)
    if cleaned.startswith("+61"):
        return cleaned
    if cleaned.startswith("61"):
        return f"+{cleaned}"
    if cleaned.startswith("0"):
        return f"+61{cleaned[1:]}"
    if cleaned.startswith("+86"):
        return cleaned
    if cleaned.startswith("86"):
        return f"+{cleaned}"
    if re.fullmatch(r"1[3-9]\d{9}", cleaned):
        return f"+86{cleaned}"
    if cleaned.startswith("+") and cleaned[1:].isdigit():
        return cleaned
    raise TwilioOtpError("INVALID_PHONE", "Phone number cannot be converted to E.164")


@lru_cache(maxsize=1)
def _twilio_client():
    try:
        from twilio.rest import Client
    except ImportError as exc:  # pragma: no cover - guarded by requirements
        raise TwilioOtpError("TWILIO_NOT_INSTALLED", "Twilio support is not installed") from exc

    account_sid = settings.twilio_account_sid.strip()
    auth_token = settings.twilio_auth_token.strip()
    if not account_sid or not auth_token:
        raise TwilioOtpError("TWILIO_NOT_CONFIGURED", "Twilio credentials are missing")
    return Client(account_sid, auth_token)


def _service():
    service_sid = settings.twilio_verify_service_sid.strip()
    if not service_sid:
        raise TwilioOtpError("TWILIO_NOT_CONFIGURED", "Twilio Verify service SID is missing")
    return _twilio_client().verify.v2.services(service_sid)


def send_sms_verification(phone: str) -> None:
    to = to_e164_phone(phone)
    try:
        _service().verifications.create(to=to, channel="sms")
    except TwilioOtpError:
        raise
    except Exception as exc:  # pragma: no cover - depends on Twilio runtime errors
        raise TwilioOtpError("TWILIO_SEND_FAILED", "Unable to send verification code") from exc


def verify_sms_code(phone: str, code: str) -> None:
    to = to_e164_phone(phone)
    try:
        result = _service().verification_checks.create(to=to, code=code.strip())
    except TwilioOtpError:
        raise
    except Exception as exc:  # pragma: no cover - depends on Twilio runtime errors
        raise TwilioOtpError("TWILIO_VERIFY_FAILED", "Unable to verify code") from exc

    if getattr(result, "status", "") != "approved":
        raise TwilioOtpError("OTP_INVALID", "Invalid verification code")
