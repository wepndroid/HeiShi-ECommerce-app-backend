"""PSP webhook handlers."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import stripe
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Order
from app.notification_jobs import enqueue_notification, notify_payment_failed
from app.payments.fulfillment import dispatch_order_paid_push, fulfill_paid_order
from app.payments.refunds import apply_paypal_refund_update, apply_stripe_refund_update


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
            refund_state = transition.status
            enqueue_notification(
                db,
                user_id=order.buyer_id,
                role="buyer",
                category="refund_update",
                notification_type=f"refund_{refund_state}",
                title={
                    "refunded": "Refund completed",
                    "pending": "Refund processing",
                }.get(refund_state, "Refund needs attention"),
                body={
                    "refunded": f"Your refund for order #{order.id} is complete.",
                    "pending": f"Your refund for order #{order.id} is still processing.",
                }.get(refund_state, f"The refund for order #{order.id} could not be completed."),
                title_zh={
                    "refunded": "退款已完成",
                    "pending": "退款处理中",
                }.get(refund_state, "退款处理异常"),
                body_zh={
                    "refunded": f"订单 #{order.id} 的退款已完成。",
                    "pending": f"订单 #{order.id} 的退款仍在处理中。",
                }.get(refund_state, f"订单 #{order.id} 的退款未能完成。"),
                business_type="order",
                business_id=str(order.id),
                deep_link=f"heymarket://order/{order.id}",
                deduplication_key=f"order:{order.id}:refund:{refund_state}:buyer",
                mandatory=True,
            )
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
    if event_type in (
        "payment_intent.payment_failed",
        "checkout.session.async_payment_failed",
        "checkout.session.expired",
    ):
        psp_id = data_object.get("payment_intent") or data_object.get("id")
        order = _order_by_psp_payment(db, "stripe", psp_id) if psp_id else None
        if not order:
            meta = data_object.get("metadata") or {}
            oid = meta.get("order_id") or data_object.get("client_reference_id")
            if oid:
                try:
                    order = _order_by_metadata(db, int(oid))
                except (TypeError, ValueError):
                    order = None
        if order and order.status == "pendingPay":
            error = data_object.get("last_payment_error") or {}
            reason = error.get("message") or f"Stripe reported {event_type}"
            order.payment_status = "failed"
            order.updated_at = datetime.now(timezone.utc)
            notify_payment_failed(
                db,
                order,
                reason=str(reason),
                event_key=str(event.get("id") or event_type),
            )
            db.commit()
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
    if event_type in (
        "PAYMENT.CAPTURE.DENIED",
        "CHECKOUT.PAYMENT-APPROVAL.REVERSED",
    ):
        psp_id = (
            resource.get("supplementary_data", {})
            .get("related_ids", {})
            .get("order_id")
            or resource.get("id")
        )
        order = _order_by_psp_payment(db, "paypal", psp_id) if psp_id else None
        if order and order.status == "pendingPay":
            order.payment_status = "failed"
            order.updated_at = datetime.now(timezone.utc)
            notify_payment_failed(
                db,
                order,
                reason=resource.get("status_details", {}).get("reason"),
                event_key=str(payload.get("id") or event_type),
            )
            db.commit()
        return True
    if event_type in (
        "PAYMENT.CAPTURE.REFUNDED",
        "PAYMENT.CAPTURE.REVERSED",
        "PAYMENT.REFUND.COMPLETED",
        "PAYMENT.REFUND.FAILED",
        "PAYMENT.REFUND.PENDING",
    ):
        related = resource.get("supplementary_data", {}).get("related_ids", {})
        capture_id = related.get("capture_id")
        refund_id = resource.get("id")
        order = None
        if refund_id:
            order = (
                db.query(Order)
                .filter(Order.psp == "paypal", Order.refund_reference == refund_id)
                .first()
            )
        if not order and capture_id:
            order = (
                db.query(Order)
                .filter(
                    Order.psp == "paypal",
                    Order.psp_transaction_id == capture_id,
                )
                .first()
            )
        if order:
            refund_resource = dict(resource)
            if event_type in {
                "PAYMENT.CAPTURE.REFUNDED",
                "PAYMENT.REFUND.COMPLETED",
            }:
                refund_resource["status"] = "COMPLETED"
            elif event_type == "PAYMENT.REFUND.FAILED":
                refund_resource["status"] = "FAILED"
            elif event_type == "PAYMENT.REFUND.PENDING":
                refund_resource["status"] = "PENDING"
            transition = apply_paypal_refund_update(order, refund_resource)
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
            enqueue_notification(
                db,
                user_id=order.buyer_id,
                role="buyer",
                category="refund_update",
                notification_type=f"refund_{transition.status}",
                title={
                    "refunded": "Refund completed",
                    "pending": "Refund processing",
                }.get(transition.status, "Refund needs attention"),
                body={
                    "refunded": f"Your refund for order #{order.id} is complete.",
                    "pending": f"Your refund for order #{order.id} is still processing.",
                }.get(
                    transition.status,
                    f"The refund for order #{order.id} could not be completed.",
                ),
                title_zh={
                    "refunded": "退款已完成",
                    "pending": "退款处理中",
                }.get(transition.status, "退款处理异常"),
                body_zh={
                    "refunded": f"订单 #{order.id} 的退款已完成。",
                    "pending": f"订单 #{order.id} 的退款仍在处理中。",
                }.get(transition.status, f"订单 #{order.id} 的退款未能完成。"),
                business_type="order",
                business_id=str(order.id),
                deep_link=f"heymarket://order/{order.id}",
                deduplication_key=(
                    f"order:{order.id}:paypal-refund:{transition.status}:"
                    f"{payload.get('id') or refund_id or 'event'}"
                ),
                mandatory=True,
            )
            db.commit()
        return True
    # Signature verification happens at the route boundary. A verified event may
    # legitimately belong to a sandbox fixture or an unrelated PayPal order, so
    # acknowledge it to prevent needless retries.
    return True
