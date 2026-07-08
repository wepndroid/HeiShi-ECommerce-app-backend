"""PSP webhook handlers."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Order
from app.payments.fulfillment import dispatch_order_paid_push, fulfill_paid_order


def _order_by_psp_payment(db: Session, psp: str, psp_payment_id: str) -> Order | None:
    return (
        db.query(Order)
        .filter(Order.psp == psp, Order.psp_payment_id == psp_payment_id)
        .first()
    )


def _order_by_metadata(db: Session, order_id: int) -> Order | None:
    return db.query(Order).filter(Order.id == order_id).first()


def handle_stripe_webhook(db: Session, payload: bytes, signature: str | None) -> bool:
    secret = settings.stripe_webhook_secret.strip()
    if secret and signature:
        parts = dict(item.split("=", 1) for item in signature.split(",") if "=" in item)
        timestamp = parts.get("t", "")
        v1 = parts.get("v1", "")
        signed = f"{timestamp}.{payload.decode('utf-8')}"
        expected = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, v1):
            return False

    event = json.loads(payload)
    event_type = event.get("type", "")
    data_object = event.get("data", {}).get("object", {})
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
    return False


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
    return False
