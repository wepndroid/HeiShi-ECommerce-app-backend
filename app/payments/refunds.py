"""Provider-backed buyer refunds for disputed orders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app import stripe_service
from app.config import settings
from app.models import Order
from app.payments.paypal_adapter import PayPalAdapter
from app.payments.service import amount_to_minor


@dataclass
class RefundTransition:
    status: str
    changed: bool = False
    reference: str | None = None
    code: str | None = None
    message: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _set_refunded(order: Order, reference: str | None = None) -> RefundTransition:
    order.payment_status = "refunded"
    order.updated_at = _now()
    return RefundTransition(status="refunded", changed=True, reference=reference)


def _set_failed(code: str, message: str) -> RefundTransition:
    return RefundTransition(status="failed", code=code, message=message)


def refund_order_payment(order: Order) -> RefundTransition:
    current = (order.payment_status or "").lower()
    if current == "refunded":
        return RefundTransition(status="refunded", reference=order.psp_transaction_id)

    if settings.payments_simulated:
        return _set_refunded(order, reference=order.psp_transaction_id or order.psp_payment_id)

    total_minor = amount_to_minor((order.amount or 0.0) + (order.escrow_fee or 0.0))
    if total_minor <= 0:
        return _set_failed("INVALID_REFUND_AMOUNT", "Order total is invalid for refund")

    if order.psp == "stripe":
        payment_intent_id = order.psp_transaction_id or order.psp_payment_id
        if not payment_intent_id:
            return _set_failed("PAYMENT_REFERENCE_MISSING", "Stripe payment reference is missing")
        try:
            refund = stripe_service.create_refund(
                payment_intent_id=payment_intent_id,
                amount_minor=total_minor,
                metadata={"order_id": str(order.id)},
            )
        except Exception as exc:
            return _set_failed("STRIPE_REFUND_FAILED", str(exc))
        return _set_refunded(order, refund.get("id"))

    if order.psp == "paypal":
        capture_id = order.psp_transaction_id
        if not capture_id:
            return _set_failed("PAYMENT_REFERENCE_MISSING", "PayPal capture reference is missing")
        try:
            refund = PayPalAdapter().refund_capture(
                capture_id,
                amount_minor=total_minor,
                currency=order.charge_currency or "AUD",
                note=f"Refund for order #{order.id}",
            )
        except Exception as exc:
            return _set_failed("PAYPAL_REFUND_FAILED", str(exc))
        return _set_refunded(order, refund.get("id"))

    return _set_failed("PSP_UNSUPPORTED", "Automatic buyer refunds are not implemented for this provider")
