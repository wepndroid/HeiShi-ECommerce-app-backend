"""Payment router — selects PSP adapter per checkout request."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Order, PaymentMethod, User
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


def start_checkout(order: Order, *, payment_method: str, db: Session | None = None) -> CheckoutResult:
    adapter = resolve_adapter(payment_method)
    currency = (order.charge_currency or "aud").lower()
    total = order.amount + (order.escrow_fee or 0.0)
    customer_id: str | None = None
    payment_method_id: str | None = None
    # For card checkout, charge the buyer's saved Stripe card via a PaymentIntent.
    if payment_method == "card" and db is not None and adapter.psp == "stripe":
        buyer = db.query(User).filter(User.id == order.buyer_id).first()
        customer_id = getattr(buyer, "stripe_customer_id", None) if buyer else None
        pm = (
            db.query(PaymentMethod)
            .filter(
                PaymentMethod.user_id == order.buyer_id,
                PaymentMethod.stripe_payment_method_id.isnot(None),
            )
            .order_by(PaymentMethod.is_default.desc())
            .first()
        )
        payment_method_id = pm.stripe_payment_method_id if pm else None
    return adapter.create_checkout(
        order_id=order.id,
        amount_minor=amount_to_minor(total),
        currency=currency,
        buyer_id=order.buyer_id,
        payment_method=payment_method,
        customer_id=customer_id,
        payment_method_id=payment_method_id,
    )


def apply_checkout_to_order(order: Order, result: CheckoutResult, payment_method: str) -> None:
    order.psp = result.psp
    order.payment_method = payment_method
    order.payment_status = result.payment_status
    order.psp_payment_id = result.psp_payment_id
    order.charge_currency = order.charge_currency or "aud"
    order.amount_minor = amount_to_minor(order.amount + (order.escrow_fee or 0.0))
