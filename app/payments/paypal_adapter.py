"""PayPal adapter — sandbox/live checkout session (PROG-409)."""

from __future__ import annotations

import httpx

from app.config import settings
from app.payments.base import CheckoutResult


class PayPalAdapter:
    psp = "paypal"

    def _base_url(self) -> str:
        return "https://api-m.sandbox.paypal.com" if settings.payments_simulated else "https://api-m.paypal.com"

    def _access_token(self) -> str:
        client_id = settings.paypal_client_id.strip()
        client_secret = settings.paypal_client_secret.strip()
        if not client_id or not client_secret:
            raise RuntimeError("PayPal credentials not configured")
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                f"{self._base_url()}/v1/oauth2/token",
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
            )
        if response.status_code >= 400:
            raise RuntimeError(f"PayPal auth failed: {response.text[:200]}")
        return response.json()["access_token"]

    def create_checkout(
        self, *, order_id: int, amount_minor: int, currency: str, buyer_id: str
    ) -> CheckoutResult:
        if not settings.paypal_client_id.strip():
            token = f"sim_{order_id}"
            return CheckoutResult(
                psp=self.psp,
                payment_status="created",
                checkout_url=f"https://www.sandbox.paypal.com/checkoutnow?token={token}",
                psp_payment_id=f"sim_pp_{order_id}",
            )

        amount = f"{amount_minor / 100:.2f}"
        body = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": str(order_id),
                    "custom_id": buyer_id,
                    "amount": {"currency_code": currency.upper(), "value": amount},
                }
            ],
            "application_context": {
                "return_url": f"{settings.base_url.rstrip('/')}/v1/payments/paypal/return?orderId={order_id}",
                "cancel_url": f"{settings.base_url.rstrip('/')}/v1/payments/paypal/cancel?orderId={order_id}",
            },
        }
        token = self._access_token()
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                f"{self._base_url()}/v2/checkout/orders",
                json=body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
        if response.status_code >= 400:
            raise RuntimeError(f"PayPal checkout failed: {response.text[:200]}")
        payload = response.json()
        approve = next((link["href"] for link in payload.get("links", []) if link.get("rel") == "approve"), None)
        return CheckoutResult(
            psp=self.psp,
            payment_status=payload.get("status", "CREATED").lower(),
            checkout_url=approve,
            psp_payment_id=payload.get("id"),
        )
