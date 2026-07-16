"""Stripe Connect adapter — live Checkout Sessions via REST (PROG-408)."""

from __future__ import annotations

import httpx

from app.config import settings
from app.payments.base import CheckoutResult


class StripeAdapter:
    psp = "stripe"

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
        secret = settings.stripe_secret_key.strip()
        if not secret:
            if not settings.payments_simulated:
                raise RuntimeError("Stripe credentials not configured")
            payment_id = f"pi_sim_{order_id}"
            return CheckoutResult(
                psp=self.psp,
                payment_status="requires_payment_method",
                client_secret=f"{payment_id}_secret_sim",
                psp_payment_id=payment_id,
            )

        if payment_method == "card" and native_payment_sheet and customer_id:
            from app import stripe_service

            try:
                intent = stripe_service.create_payment_sheet_intent(
                    amount_minor=amount_minor,
                    currency=currency,
                    customer_id=customer_id,
                    description=f"HeyMarket order #{order_id}",
                    metadata={"order_id": str(order_id), "buyer_id": buyer_id},
                    transfer_group=f"order_{order_id}",
                )
                ephemeral_key = stripe_service.create_customer_ephemeral_key(customer_id)
            except Exception as exc:
                raise RuntimeError(f"Stripe PaymentSheet setup failed: {exc}") from exc
            return CheckoutResult(
                psp=self.psp,
                payment_status=intent.get("status", "requires_payment_method"),
                client_secret=intent.get("client_secret"),
                psp_payment_id=intent.get("id"),
                publishable_key=settings.stripe_publishable_key.strip(),
                customer_id=customer_id,
                ephemeral_key=ephemeral_key,
            )

        # Saved-card server confirmation remains available to non-PaymentSheet callers.
        if payment_method == "card" and customer_id and payment_method_id:
            from app import stripe_service

            try:
                intent = stripe_service.create_and_confirm_payment_intent(
                    amount_minor=amount_minor,
                    currency=currency,
                    customer_id=customer_id,
                    payment_method_id=payment_method_id,
                    description=f"HeyMarket order #{order_id}",
                    metadata={"order_id": str(order_id), "buyer_id": buyer_id},
                    transfer_group=f"order_{order_id}",
                )
            except Exception as exc:  # surfaced as PAYMENT_PROVIDER_ERROR by the router
                raise RuntimeError(f"Stripe charge failed: {exc}") from exc
            return CheckoutResult(
                psp=self.psp,
                payment_status=intent.get("status", "requires_payment_method"),
                client_secret=intent.get("client_secret"),
                psp_payment_id=intent.get("id"),
            )

        # Fallback: hosted Checkout Session (redirect) — wallets or no saved card.
        base = settings.base_url.rstrip("/")
        data = {
            "mode": "payment",
            "success_url": f"{base}/v1/payments/stripe/return?orderId={order_id}&session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{base}/v1/payments/stripe/cancel?orderId={order_id}",
            "client_reference_id": str(order_id),
            "metadata[order_id]": str(order_id),
            "metadata[buyer_id]": buyer_id,
            "metadata[selected_payment_method]": payment_method,
            "payment_intent_data[transfer_group]": f"order_{order_id}",
            "line_items[0][price_data][currency]": currency.lower(),
            "line_items[0][price_data][unit_amount]": str(amount_minor),
            "line_items[0][price_data][product_data][name]": f"HeyMarket order #{order_id}",
            "line_items[0][quantity]": "1",
        }
        if payment_method == "alipay":
            data["payment_method_types[0]"] = "alipay"
        elif payment_method == "wechat":
            data["payment_method_types[0]"] = "wechat_pay"
        else:
            # Card checkout on Stripe-hosted Checkout also surfaces Apple Pay / Google Pay
            # automatically when the account, browser, and device are eligible.
            data["payment_method_types[0]"] = "card"
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
