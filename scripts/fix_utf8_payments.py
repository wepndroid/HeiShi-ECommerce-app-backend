from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app" / "payments"

STRIPE = '''"""Stripe Connect adapter — live PaymentIntents via REST (PROG-408)."""

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

        data = {
            "amount": str(amount_minor),
            "currency": currency.lower(),
            "metadata[order_id]": str(order_id),
            "metadata[buyer_id]": buyer_id,
            "automatic_payment_methods[enabled]": "true",
        }
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                "https://api.stripe.com/v1/payment_intents",
                data=data,
                auth=(secret, ""),
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Stripe checkout failed: {response.text[:200]}")
        payload = response.json()
        return CheckoutResult(
            psp=self.psp,
            payment_status=payload.get("status", "requires_payment_method"),
            client_secret=payload.get("client_secret"),
            psp_payment_id=payload.get("id"),
        )
'''

for name, content in [
    ("stripe_adapter.py", STRIPE),
]:
    path = ROOT / name
    path.write_text(content, encoding="utf-8")
    print("fixed", path)
