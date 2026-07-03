"""Stripe Connect adapter — live Checkout Sessions via REST (PROG-408)."""

from __future__ import annotations

import httpx

from app.config import settings
from app.payments.base import CheckoutResult


class StripeAdapter:
    psp = "stripe"

    def create_checkout(
        self, *, order_id: int, amount_minor: int, currency: str, buyer_id: str
    ) -> CheckoutResult:
        secret = settings.stripe_secret_key.strip()
        if not secret:
            payment_id = f"pi_sim_{order_id}"
            return CheckoutResult(
                psp=self.psp,
                payment_status="requires_payment_method",
                client_secret=f"{payment_id}_secret_sim",
                psp_payment_id=payment_id,
            )

        base = settings.base_url.rstrip("/")
        data = {
            "mode": "payment",
            "success_url": f"{base}/v1/payments/stripe/return?orderId={order_id}&session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{base}/v1/payments/stripe/cancel?orderId={order_id}",
            "client_reference_id": str(order_id),
            "metadata[order_id]": str(order_id),
            "metadata[buyer_id]": buyer_id,
            "line_items[0][price_data][currency]": currency.lower(),
            "line_items[0][price_data][unit_amount]": str(amount_minor),
            "line_items[0][price_data][product_data][name]": f"HeyMarket order #{order_id}",
            "line_items[0][quantity]": "1",
        }
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                "https://api.stripe.com/v1/checkout/sessions",
                data=data,
                auth=(secret, ""),
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Stripe checkout failed: {response.text[:200]}")
        payload = response.json()
        return CheckoutResult(
            psp=self.psp,
            payment_status=payload.get("status", "open"),
            checkout_url=payload.get("url"),
            psp_payment_id=payload.get("id"),
        )
