"""PayPal Commerce Platform seller onboarding and delayed disbursement calls."""

from __future__ import annotations

import base64
import json

import httpx

from app.config import settings


class PayPalPartnerError(RuntimeError):
    """Raised when PayPal rejects a partner operation."""


def _base_url() -> str:
    return "https://api-m.sandbox.paypal.com" if settings.paypal_sandbox else "https://api-m.paypal.com"


def access_token() -> str:
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{_base_url()}/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(settings.paypal_client_id.strip(), settings.paypal_client_secret.strip()),
        )
    if response.status_code >= 400:
        raise PayPalPartnerError(f"PayPal authentication failed: {response.text[:300]}")
    token = response.json().get("access_token")
    if not token:
        raise PayPalPartnerError("PayPal authentication response did not include an access token")
    return token


def partner_headers(*, merchant_id: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token()}",
        "Content-Type": "application/json",
    }
    attribution_id = settings.paypal_partner_attribution_id.strip()
    if attribution_id:
        headers["PayPal-Partner-Attribution-Id"] = attribution_id
    if merchant_id:
        headers["PayPal-Auth-Assertion"] = auth_assertion(merchant_id)
    return headers


def auth_assertion(merchant_id: str) -> str:
    def encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode({'iss': settings.paypal_client_id.strip(), 'payer_id': merchant_id})}."


def create_seller_referral(*, tracking_id: str, return_url: str) -> dict:
    body = {
        "tracking_id": tracking_id,
        "operations": [
            {
                "operation": "API_INTEGRATION",
                "api_integration_preference": {
                    "rest_api_integration": {
                        "integration_method": "PAYPAL",
                        "integration_type": "THIRD_PARTY",
                        "third_party_details": {
                            "features": [
                                "PAYMENT",
                                "REFUND",
                                "PARTNER_FEE",
                                "DELAY_FUNDS_DISBURSEMENT",
                            ]
                        },
                    }
                },
            }
        ],
        "products": ["PPCP"],
        "legal_consents": [{"type": "SHARE_DATA_CONSENT", "granted": True}],
        "legal_country_code": "AU",
        "partner_config_override": {
            "return_url": return_url,
            "return_url_description": "Return to HeyMarket",
        },
    }
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{_base_url()}/v2/customer/partner-referrals",
            json=body,
            headers=partner_headers(),
        )
    if response.status_code >= 400:
        raise PayPalPartnerError(f"PayPal seller onboarding failed: {response.text[:500]}")
    return response.json()


def referral_action_url(payload: dict) -> str:
    for link in payload.get("links", []):
        if link.get("rel") == "action_url" and link.get("href"):
            return str(link["href"])
    raise PayPalPartnerError("PayPal onboarding response did not include an action URL")


def merchant_integration(merchant_id: str) -> dict | None:
    partner_id = settings.paypal_partner_merchant_id.strip()
    if not partner_id:
        return None
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            f"{_base_url()}/v1/customer/partners/{partner_id}/merchant-integrations/{merchant_id}",
            headers=partner_headers(merchant_id=merchant_id),
        )
    if response.status_code >= 400:
        raise PayPalPartnerError(f"PayPal merchant verification failed: {response.text[:500]}")
    return response.json()


def create_referenced_payout(capture_id: str) -> dict:
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{_base_url()}/v1/payments/referenced-payouts-items",
            json={"reference_id": capture_id, "reference_type": "TRANSACTION_ID"},
            headers=partner_headers(),
        )
    if response.status_code >= 400:
        raise PayPalPartnerError(f"PayPal delayed disbursement failed: {response.text[:500]}")
    return response.json()
