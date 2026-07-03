"""Payment router — selects PSP adapter per checkout request."""

from __future__ import annotations

from app.models import Order
from app.payments.base import CheckoutResult
from app.payments.paypal_adapter import PayPalAdapter
from app.payments.stripe_adapter import StripeAdapter

_ADAPTERS = {
    "stripe": StripeAdapter(),
    "paypal": PayPalAdapter(),
}


def resolve_adapter(method: str) -> StripeAdapter | PayPalAdapter:
    if method == "paypal":
        return _ADAPTERS["paypal"]
    return _ADAPTERS["stripe"]


def amount_to_minor(amount: float) -> int:
    return int(round(amount * 100))


def start_checkout(order: Order, *, payment_method: str) -> CheckoutResult:
    adapter = resolve_adapter(payment_method)
    currency = (order.charge_currency or "aud").lower()
    total = order.amount + (order.escrow_fee or 0.0)
    return adapter.create_checkout(
        order_id=order.id,
        amount_minor=amount_to_minor(total),
        currency=currency,
        buyer_id=order.buyer_id,
    )


def apply_checkout_to_order(order: Order, result: CheckoutResult, payment_method: str) -> None:
    order.psp = result.psp
    order.payment_method = payment_method
    order.payment_status = result.payment_status
    order.psp_payment_id = result.psp_payment_id
    order.charge_currency = order.charge_currency or "aud"
    order.amount_minor = amount_to_minor(order.amount + (order.escrow_fee or 0.0))
