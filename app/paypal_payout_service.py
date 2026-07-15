"""Thin PayPal Payouts wrapper for seller disbursements."""

from __future__ import annotations

import httpx

from app.config import settings


class PayPalPayoutError(RuntimeError):
    """Raised when a real PayPal payout call fails."""


def _base_url() -> str:
    return "https://api-m.sandbox.paypal.com" if settings.paypal_sandbox else "https://api-m.paypal.com"


def _credentials() -> tuple[str, str]:
    client_id = settings.paypal_client_id.strip()
    client_secret = settings.paypal_client_secret.strip()
    if not client_id or not client_secret:
        raise PayPalPayoutError("PayPal payout credentials are not configured")
    return client_id, client_secret


def _access_token() -> str:
    client_id, client_secret = _credentials()
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{_base_url()}/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(client_id, client_secret),
        )
    if response.status_code >= 400:
        raise PayPalPayoutError(f"PayPal auth failed: {response.text[:300]}")
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise PayPalPayoutError("PayPal auth response did not include an access token")
    return token


def create_payout(
    *,
    sender_batch_id: str,
    sender_item_id: str,
    receiver: str,
    amount_minor: int,
    currency: str,
    note: str,
    email_subject: str = "You have a payout",
) -> dict:
    amount_value = f"{amount_minor / 100:.2f}"
    body = {
        "sender_batch_header": {
            "sender_batch_id": sender_batch_id,
            "email_subject": email_subject,
        },
        "items": [
            {
                "recipient_type": "EMAIL",
                "amount": {
                    "value": amount_value,
                    "currency": currency.upper(),
                },
                "receiver": receiver,
                "note": note,
                "sender_item_id": sender_item_id,
            }
        ],
    }
    token = _access_token()
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{_base_url()}/v1/payments/payouts",
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "PayPal-Request-Id": sender_batch_id,
            },
        )
    if response.status_code >= 400:
        raise PayPalPayoutError(f"PayPal payout creation failed: {response.text[:300]}")
    return response.json()


def retrieve_payout_batch(payout_batch_id: str) -> dict:
    token = _access_token()
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            f"{_base_url()}/v1/payments/payouts/{payout_batch_id}",
            params={"page_size": 20, "page": 1, "total_required": "true"},
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code >= 400:
        raise PayPalPayoutError(f"PayPal payout lookup failed: {response.text[:300]}")
    return response.json()
