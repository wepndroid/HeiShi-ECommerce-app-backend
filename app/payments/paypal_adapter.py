"""PayPal adapter — sandbox/live checkout session (PROG-409)."""

from __future__ import annotations

import httpx

from app.config import settings
from app import paypal_partner_service
from app.payments.base import CheckoutResult


class PayPalAdapter:
    psp = "paypal"

    def _base_url(self) -> str:
        return "https://api-m.sandbox.paypal.com" if settings.paypal_sandbox else "https://api-m.paypal.com"

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

    def verify_webhook_signature(self, headers: dict[str, str], event: dict) -> bool:
        """Verify a webhook with PayPal before allowing it to mutate an order."""
        webhook_id = settings.paypal_webhook_id.strip()
        required = {
            "auth_algo": headers.get("paypal-auth-algo", ""),
            "cert_url": headers.get("paypal-cert-url", ""),
            "transmission_id": headers.get("paypal-transmission-id", ""),
            "transmission_sig": headers.get("paypal-transmission-sig", ""),
            "transmission_time": headers.get("paypal-transmission-time", ""),
        }
        if not webhook_id or not all(required.values()):
            return False

        body = {
            **required,
            "webhook_id": webhook_id,
            "webhook_event": event,
        }
        try:
            token = self._access_token()
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    f"{self._base_url()}/v1/notifications/verify-webhook-signature",
                    json=body,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                )
            return response.status_code < 400 and response.json().get("verification_status") == "SUCCESS"
        except (httpx.HTTPError, RuntimeError, ValueError):
            return False

    def create_checkout(
        self,
        *,
        order_id: int,
        amount_minor: int,
        currency: str,
        buyer_id: str,
        payment_method: str,
        customer_id: str | None = None,
        payment_method_id: str | None = None,
        native_payment_sheet: bool = False,
        payee_merchant_id: str | None = None,
        platform_fee_minor: int = 0,
    ) -> CheckoutResult:
        # customer_id / payment_method_id are Stripe-only; PayPal ignores them.
        if settings.payments_simulated and not settings.paypal_client_id.strip():
            token = f"sim_{order_id}"
            return CheckoutResult(
                psp=self.psp,
                payment_status="created",
                checkout_url=f"https://www.sandbox.paypal.com/checkoutnow?token={token}",
                psp_payment_id=f"sim_pp_{order_id}",
            )

        amount = f"{amount_minor / 100:.2f}"
        purchase_unit = {
            "reference_id": str(order_id),
            "custom_id": buyer_id,
            "amount": {"currency_code": currency.upper(), "value": amount},
        }
        headers = {"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json"}
        if payee_merchant_id:
            purchase_unit["payee"] = {"merchant_id": payee_merchant_id}
            instruction: dict = {"disbursement_mode": "DELAYED"}
            if platform_fee_minor > 0:
                instruction["platform_fees"] = [
                    {
                        "amount": {
                            "currency_code": currency.upper(),
                            "value": f"{platform_fee_minor / 100:.2f}",
                        }
                    }
                ]
            purchase_unit["payment_instruction"] = instruction
            headers.update(
                {
                    "PayPal-Auth-Assertion": paypal_partner_service.auth_assertion(payee_merchant_id),
                    "PayPal-Partner-Attribution-Id": settings.paypal_partner_attribution_id.strip(),
                }
            )
        body = {
            "intent": "CAPTURE",
            "purchase_units": [purchase_unit],
            "application_context": {
                "return_url": f"{settings.base_url.rstrip('/')}/v1/payments/paypal/return?orderId={order_id}",
                "cancel_url": f"{settings.base_url.rstrip('/')}/v1/payments/paypal/cancel?orderId={order_id}",
            },
        }
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                f"{self._base_url()}/v2/checkout/orders",
                json=body,
                headers=headers,
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

    def capture_order(self, paypal_order_id: str, payee_merchant_id: str | None = None) -> dict:
        token = self._access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if payee_merchant_id:
            headers.update(
                {
                    "PayPal-Auth-Assertion": paypal_partner_service.auth_assertion(payee_merchant_id),
                    "PayPal-Partner-Attribution-Id": settings.paypal_partner_attribution_id.strip(),
                }
            )
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                f"{self._base_url()}/v2/checkout/orders/{paypal_order_id}/capture",
                headers=headers,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"PayPal capture failed: {response.text[:200]}")
        return response.json()

    def refund_capture(
        self,
        capture_id: str,
        *,
        amount_minor: int | None = None,
        currency: str = "AUD",
        note: str | None = None,
        payee_merchant_id: str | None = None,
    ) -> dict:
        token = self._access_token()
        body: dict = {}
        if amount_minor is not None:
            body["amount"] = {
                "currency_code": currency.upper(),
                "value": f"{amount_minor / 100:.2f}",
            }
        if note:
            body["note_to_payer"] = note
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if payee_merchant_id:
            headers.update(
                {
                    "PayPal-Auth-Assertion": paypal_partner_service.auth_assertion(payee_merchant_id),
                    "PayPal-Partner-Attribution-Id": settings.paypal_partner_attribution_id.strip(),
                }
            )
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                f"{self._base_url()}/v2/payments/captures/{capture_id}/refund",
                json=body or None,
                headers=headers,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"PayPal refund failed: {response.text[:200]}")
        return response.json()
