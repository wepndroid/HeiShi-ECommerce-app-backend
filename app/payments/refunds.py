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
    now = _now()
    order.refund_status = "succeeded"
    order.refund_reference = reference or order.refund_reference
    order.refund_failure_code = None
    order.refund_failure_reason = None
    order.refunded_at = order.refunded_at or now
    # A successful provider refund permanently closes any seller payout that has
    # not already been released. Keep this transition in the shared refund
    # helper so admin disputes and any future refund entry point cannot leave a
    # misleading `pending` payout behind.
    if order.payout_status != "released":
        order.payout_status = "reversed"
        order.payout_reversed_at = order.payout_reversed_at or now
        order.payout_reversal_reference = order.payout_reversal_reference or reference
        order.payout_failure_code = None
        order.payout_failure_reason = None
        order.payout_failed_at = None
    order.updated_at = now
    return RefundTransition(status="refunded", changed=True, reference=order.refund_reference)


def _set_pending(order: Order, reference: str | None) -> RefundTransition:
    order.refund_status = "pending"
    order.refund_reference = reference or order.refund_reference
    order.refund_failure_code = None
    order.refund_failure_reason = None
    order.refunded_at = None
    order.payout_paused = True
    if order.payout_status != "released":
        order.payout_status = "blocked"
        order.payout_failure_code = "REFUND_PENDING"
        order.payout_failure_reason = "Seller payout is blocked while the buyer refund is pending"
    order.updated_at = _now()
    return RefundTransition(status="pending", changed=True, reference=order.refund_reference)


def _set_failed(
    code: str,
    message: str,
    *,
    order: Order | None = None,
    reference: str | None = None,
) -> RefundTransition:
    if order is not None:
        order.refund_status = "failed"
        order.refund_reference = reference or order.refund_reference
        order.refund_failure_code = code
        order.refund_failure_reason = message
        order.refunded_at = None
        order.payout_paused = True
        if order.payout_status != "released":
            order.payout_status = "blocked"
            order.payout_failure_code = "REFUND_FAILED"
            order.payout_failure_reason = "Seller payout remains blocked because the buyer refund failed"
        order.updated_at = _now()
    return RefundTransition(
        status="failed",
        changed=order is not None,
        reference=reference,
        code=code,
        message=message,
    )


def apply_stripe_refund_update(order: Order, refund: dict) -> RefundTransition:
    """Apply a Stripe Refund object's provider status to the local order."""

    reference = refund.get("id") or order.refund_reference
    status = str(refund.get("status") or "").lower()
    if status == "succeeded":
        return _set_refunded(order, reference)
    if status in {"failed", "canceled"}:
        reason = refund.get("failure_reason") or f"Stripe refund {status}"
        return _set_failed(
            "STRIPE_REFUND_FAILED",
            str(reason),
            order=order,
            reference=reference,
        )
    # Stripe can create asynchronous refunds in a pending state. Unknown
    # non-terminal statuses are deliberately treated as pending rather than
    # incorrectly telling the buyer that their money has been returned.
    return _set_pending(order, reference)


def refund_order_payment(order: Order) -> RefundTransition:
    current = (order.payment_status or "").lower()
    if current == "refunded" or order.refund_status == "succeeded":
        return RefundTransition(status="refunded", reference=order.refund_reference)
    if order.refund_status == "pending":
        return RefundTransition(status="pending", reference=order.refund_reference)

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
        return apply_stripe_refund_update(order, refund)

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
                payee_merchant_id=getattr(order, "paypal_payee_merchant_id", None),
            )
        except Exception as exc:
            return _set_failed("PAYPAL_REFUND_FAILED", str(exc))
        return _set_refunded(order, refund.get("id"))

    return _set_failed("PSP_UNSUPPORTED", "Automatic buyer refunds are not implemented for this provider")
