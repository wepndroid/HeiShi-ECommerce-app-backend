"""PSP webhook handlers."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import stripe
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Order
from app.payments.fulfillment import dispatch_order_paid_push, fulfill_paid_order
from app.payments.refunds import apply_stripe_refund_update


def _order_by_psp_payment(db: Session, psp: str, psp_payment_id: str) -> Order | None:
    return (
        db.query(Order)
        .filter(Order.psp == psp, Order.psp_payment_id == psp_payment_id)
        .first()
    )


def _order_by_metadata(db: Session, order_id: int) -> Order | None:
    return db.query(Order).filter(Order.id == order_id).first()


def _order_for_stripe_refund(db: Session, refund: dict) -> Order | None:
    metadata = refund.get("metadata") or {}
    order_id = metadata.get("order_id")
    if order_id:
        try:
            order = _order_by_metadata(db, int(order_id))
        except (TypeError, ValueError):
            order = None
        if order:
            return order
    refund_id = refund.get("id")
    if refund_id:
        order = (
            db.query(Order)
            .filter(Order.psp == "stripe", Order.refund_reference == refund_id)
            .first()
        )
        if order:
            return order
    payment_intent_id = refund.get("payment_intent")
    if payment_intent_id:
        return (
            db.query(Order)
            .filter(
                Order.psp == "stripe",
                (Order.psp_transaction_id == payment_intent_id)
                | (Order.psp_payment_id == payment_intent_id),
            )
            .first()
        )
    return None


def handle_stripe_webhook(db: Session, payload: bytes, signature: str | None) -> bool:
    secret = settings.stripe_webhook_secret.strip()
    if secret:
        if not signature:
            return False
        try:
            event = stripe.Webhook.construct_event(payload, signature, secret)
        except (ValueError, stripe.error.SignatureVerificationError):
            return False
    else:
        event = json.loads(payload)
    event_type = event.get("type", "")
    data_object = event.get("data", {}).get("object", {})
    if event_type in ("refund.created", "refund.updated", "refund.failed"):
        order = _order_for_stripe_refund(db, data_object)
        if order:
            transition = apply_stripe_refund_update(order, data_object)
            if transition.status == "refunded":
                order.status = "refunded"
                order.dispute_status = "resolved"
                order.payout_paused = False
            elif transition.status == "pending":
                order.status = "refundInProgress"
                order.dispute_status = "refund_pending"
                order.payout_paused = True
            else:
                order.status = "inDispute"
                order.dispute_status = "refund_failed"
                order.payout_paused = True
            db.commit()
        return True
    if event_type in ("payment_intent.succeeded", "checkout.session.completed"):
        psp_id = data_object.get("id")
        if event_type == "checkout.session.completed":
            psp_id = data_object.get("payment_intent") or data_object.get("id")
        order = _order_by_psp_payment(db, "stripe", psp_id) if psp_id else None
        if not order:
            meta = data_object.get("metadata") or {}
            oid = meta.get("order_id") or data_object.get("client_reference_id")
            if oid:
                order = _order_by_metadata(db, int(oid))
        if order and order.status == "pendingPay":
            order.payment_status = "succeeded"
            order.psp_transaction_id = psp_id
            order.updated_at = datetime.now(timezone.utc)
            fulfill_paid_order(db, order)
            dispatch_order_paid_push(db, order.id)
            return True
    # A valid Stripe event can legitimately refer to a PaymentIntent that was
    # created outside HeyMarket (for example, `stripe trigger` fixtures). Acknowledge
    # verified but irrelevant events so Stripe does not retry them indefinitely.
    return True


def handle_paypal_webhook(db: Session, payload: dict) -> bool:
    event_type = payload.get("event_type", "")
    resource = payload.get("resource", {})
    if event_type in ("CHECKOUT.ORDER.APPROVED", "PAYMENT.CAPTURE.COMPLETED"):
        psp_id = resource.get("id") or resource.get("supplementary_data", {}).get("related_ids", {}).get("order_id")
        order = _order_by_psp_payment(db, "paypal", psp_id) if psp_id else None
        if not order:
            for unit in resource.get("purchase_units", []):
                ref = unit.get("reference_id")
                if ref:
                    order = _order_by_metadata(db, int(ref))
                    break
        if event_type == "CHECKOUT.ORDER.APPROVED":
            if order and order.status == "pendingPay":
                order.payment_status = "approved"
                order.updated_at = datetime.now(timezone.utc)
                db.commit()
            return True
        if order and order.status == "pendingPay":
            order.payment_status = "succeeded"
            order.psp_transaction_id = psp_id
            order.updated_at = datetime.now(timezone.utc)
            fulfill_paid_order(db, order)
            dispatch_order_paid_push(db, order.id)
            return True
    # Signature verification happens at the route boundary. A verified event may
    # legitimately belong to a sandbox fixture or an unrelated PayPal order, so
    # acknowledge it to prevent needless retries.
    return True
