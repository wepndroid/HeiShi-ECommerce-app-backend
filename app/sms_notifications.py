"""Optional transactional SMS delivery through Twilio Messaging."""

from __future__ import annotations

import logging

from app.config import settings
from app.twilio_otp import TwilioOtpError, to_e164_phone

logger = logging.getLogger(__name__)


def send_transaction_sms(*, phone: str | None, body: str) -> tuple[bool, str | None]:
    if not phone:
        return False, "PHONE_NOT_AVAILABLE"
    if not settings.twilio_account_sid.strip() or not settings.twilio_auth_token.strip():
        return False, "TWILIO_NOT_CONFIGURED"
    messaging_service_sid = settings.twilio_messaging_service_sid.strip()
    from_phone = settings.twilio_from_phone.strip()
    if not messaging_service_sid and not from_phone:
        return False, "TWILIO_SENDER_NOT_CONFIGURED"
    try:
        from twilio.rest import Client

        destination = to_e164_phone(phone)
        kwargs: dict[str, str] = {"to": destination, "body": body[:1500]}
        if messaging_service_sid:
            kwargs["messaging_service_sid"] = messaging_service_sid
        else:
            kwargs["from_"] = from_phone
        Client(
            settings.twilio_account_sid.strip(),
            settings.twilio_auth_token.strip(),
        ).messages.create(**kwargs)
        return True, None
    except TwilioOtpError as exc:
        return False, exc.code
    except Exception as exc:  # pragma: no cover - provider/network dependent
        logger.warning("Twilio transaction SMS failed: %s", exc)
        return False, "SMS_PROVIDER_ERROR"
