"""Provider-aware payout release helpers for completed escrow orders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app import (
    alipay_payout_service,
    paypal_partner_service,
    paypal_payout_service,
    stripe_service,
    wechat_payout_service,
)
from app.config import settings
from app.models import Order, PayoutMethod, User
from app.payments.service import amount_to_minor

PAYOUT_PENDING = "pending"
PAYOUT_PROCESSING = "processing"
PAYOUT_BLOCKED = "blocked"
PAYOUT_RELEASED = "released"
PAYOUT_FAILED = "failed"
PAYOUT_REVERSED = "reversed"

ELIGIBLE_RELEASE_STATUSES = {"pendingReview", "completed"}
SUCCESSFUL_PAYMENT_STATUSES = {"succeeded", "paid"}
ASYNC_PAYOUT_PROVIDERS = {"paypal", "alipay", "wechat"}


@dataclass
class PayoutTransition:
    status: str
    changed: bool = False
    reference: str | None = None
    reason: str | None = None
    code: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def payout_transfer_group(order_id: int) -> str:
    return f"order_{order_id}"


def paypal_sender_batch_id(order_id: int) -> str:
    return f"order-{order_id}-paypal-payout"


def alipay_out_biz_no(order_id: int) -> str:
    return f"hm-order-{order_id}-ali"


def wechat_out_batch_no(order_id: int) -> str:
    return f"hmorder{order_id}wx"


def _preferred_method(db: Session, seller_id: str, method_type: str) -> PayoutMethod | None:
    return (
        db.query(PayoutMethod)
        .filter(PayoutMethod.user_id == seller_id, PayoutMethod.type == method_type)
        .order_by(PayoutMethod.is_default.desc(), PayoutMethod.id.asc())
        .first()
    )


def _preferred_release_method(db: Session, seller_id: str, payment_provider: str | None) -> PayoutMethod | None:
    rows = (
        db.query(PayoutMethod)
        .filter(PayoutMethod.user_id == seller_id)
        .order_by(PayoutMethod.is_default.desc(), PayoutMethod.id.asc())
        .all()
    )
    if not rows:
        return None

    # Route each payout on the same rail that collected that order's buyer
    # payment. This lets one seller receive concurrent Stripe and PayPal orders
    # without manually changing a global default between releases.
    provider_method_types = {
        "stripe": {"bank"},
        "paypal": {"paypal"},
        "alipay": {"alipay"},
        "wechat": {"wechat"},
    }
    required_types = provider_method_types.get((payment_provider or "").strip().lower())
    if required_types:
        matching = [row for row in rows if row.type in required_types]
        if not matching:
            return None
        return next((row for row in matching if row.is_default), matching[0])

    default = next((row for row in rows if row.is_default), None)
    return default or rows[0]


def _set_blocked(order: Order, code: str, reason: str) -> PayoutTransition:
    order.payout_status = PAYOUT_BLOCKED
    order.payout_failure_code = code
    order.payout_failure_reason = reason
    order.payout_failed_at = None
    return PayoutTransition(status=PAYOUT_BLOCKED, changed=True, code=code, reason=reason)


def _set_failed(order: Order, code: str, reason: str) -> PayoutTransition:
    order.payout_status = PAYOUT_FAILED
    order.payout_failure_code = code
    order.payout_failure_reason = reason
    order.payout_failed_at = _now()
    return PayoutTransition(status=PAYOUT_FAILED, changed=True, code=code, reason=reason)


def _set_processing(
    order: Order,
    *,
    provider: str,
    payout_method_id: str | None,
    reference: str | None,
) -> PayoutTransition:
    order.payout_status = PAYOUT_PROCESSING
    order.payout_provider = provider
    order.payout_method_id = payout_method_id
    order.payout_reference = reference
    order.payout_failure_code = None
    order.payout_failure_reason = None
    order.payout_failed_at = None
    return PayoutTransition(status=PAYOUT_PROCESSING, changed=True, reference=reference)


def _set_released(order: Order, provider: str, payout_method: PayoutMethod, reference: str | None) -> PayoutTransition:
    order.payout_status = PAYOUT_RELEASED
    order.payout_provider = provider
    order.payout_method_id = payout_method.id
    order.payout_reference = reference
    order.payout_failure_code = None
    order.payout_failure_reason = None
    order.payout_failed_at = None
    order.payout_released_at = order.payout_released_at or _now()
    return PayoutTransition(status=PAYOUT_RELEASED, changed=True, reference=reference)


def _stripe_source_reference(order: Order) -> str | None:
    if order.psp_transaction_id and order.psp_transaction_id.startswith(("pi_", "cs_")):
        return order.psp_transaction_id
    if order.psp_payment_id and order.psp_payment_id.startswith(("pi_", "cs_")):
        return order.psp_payment_id
    return None


def _require_cny(order: Order, provider_label: str) -> str | None:
    currency = (order.charge_currency or "aud").lower()
    if currency != "cny":
        return (
            f"{provider_label} payouts currently require CNY-settled orders because "
            "the platform does not perform currency conversion"
        )
    return None


def _extract_paypal_error(payload: dict) -> tuple[str | None, str | None]:
    item = ((payload.get("items") or [None])[0]) or {}
    error = item.get("errors") or payload.get("errors") or {}
    if isinstance(error, list):
        error = error[0] if error else {}
    code = error.get("name") or error.get("issue")
    message = error.get("message") or error.get("description")
    return code, message


def _apply_paypal_batch_status(order: Order, payout_method: PayoutMethod, payload: dict) -> PayoutTransition:
    batch_header = payload.get("batch_header") or {}
    batch_id = batch_header.get("payout_batch_id") or order.payout_reference
    batch_status = (batch_header.get("batch_status") or "").upper()
    items = payload.get("items") or []
    item = items[0] if items else {}
    item_status = (item.get("transaction_status") or "").upper()
    code, message = _extract_paypal_error(payload)
    status_key = item_status or batch_status

    if status_key == "SUCCESS":
        return _set_released(order, "paypal", payout_method, batch_id)

    if status_key in {"REFUNDED", "REVERSED", "CANCELED", "CANCELLED"}:
        order.payout_status = PAYOUT_REVERSED
        order.payout_provider = "paypal"
        order.payout_method_id = payout_method.id
        order.payout_reference = batch_id
        order.payout_failure_code = code
        order.payout_failure_reason = message
        order.payout_failed_at = None
        order.payout_reversed_at = order.payout_reversed_at or _now()
        return PayoutTransition(status=PAYOUT_REVERSED, changed=True, reference=batch_id, code=code, reason=message)

    if status_key in {"BLOCKED", "ONHOLD", "HELD"}:
        order.payout_status = PAYOUT_BLOCKED
        order.payout_provider = "paypal"
        order.payout_method_id = payout_method.id
        order.payout_reference = batch_id
        order.payout_failure_code = code or status_key
        order.payout_failure_reason = message or "PayPal has held this payout for review"
        order.payout_failed_at = None
        return PayoutTransition(
            status=PAYOUT_BLOCKED,
            changed=True,
            reference=batch_id,
            code=order.payout_failure_code,
            reason=order.payout_failure_reason,
        )

    if status_key in {"FAILED", "DENIED", "RETURNED"}:
        order.payout_status = PAYOUT_FAILED
        order.payout_provider = "paypal"
        order.payout_method_id = payout_method.id
        order.payout_reference = batch_id
        order.payout_failure_code = code or status_key
        order.payout_failure_reason = message or "PayPal payout failed"
        order.payout_failed_at = _now()
        return PayoutTransition(
            status=PAYOUT_FAILED,
            changed=True,
            reference=batch_id,
            code=order.payout_failure_code,
            reason=order.payout_failure_reason,
        )

    return _set_processing(order, provider="paypal", payout_method_id=payout_method.id, reference=batch_id)


def _apply_alipay_status(order: Order, payout_method: PayoutMethod, payload: dict) -> PayoutTransition:
    reference = payload.get("order_id") or payload.get("pay_fund_order_id") or order.payout_reference or alipay_out_biz_no(order.id)
    status = (payload.get("status") or payload.get("order_status") or "").upper()
    if status == "SUCCESS":
        return _set_released(order, "alipay", payout_method, reference)
    if status in {"DEALING", "PROCESSING", "WAIT_PAY"}:
        return _set_processing(order, provider="alipay", payout_method_id=payout_method.id, reference=reference)
    if status in {"FAIL", "FAILED", "CLOSED"}:
        return _set_failed(order, "ALIPAY_PAYOUT_FAILED", payload.get("fail_reason") or "Alipay payout failed")
    return _set_processing(order, provider="alipay", payout_method_id=payout_method.id, reference=reference)


def _apply_wechat_status(order: Order, payout_method: PayoutMethod, payload: dict) -> PayoutTransition:
    reference = payload.get("batch_id") or order.payout_reference or wechat_out_batch_no(order.id)
    status = (payload.get("batch_status") or payload.get("status") or "").upper()
    if status in {"FINISHED", "SUCCESS"}:
        return _set_released(order, "wechat", payout_method, reference)
    if status in {"ACCEPTED", "PROCESSING", "WAIT_PAY", "PENDING"}:
        return _set_processing(order, provider="wechat", payout_method_id=payout_method.id, reference=reference)
    if status in {"CLOSED", "FAILED"}:
        return _set_failed(order, "WECHAT_PAYOUT_FAILED", payload.get("fail_reason") or "WeChat payout failed")
    return _set_processing(order, provider="wechat", payout_method_id=payout_method.id, reference=reference)


def _sync_provider_payout(order: Order, payout_method: PayoutMethod) -> PayoutTransition:
    provider = order.payout_provider or payout_method.type
    try:
        if provider == "paypal":
            payload = paypal_payout_service.retrieve_payout_batch(order.payout_reference or "")
            return _apply_paypal_batch_status(order, payout_method, payload)
        if provider == "alipay":
            payload = alipay_payout_service.query_transfer(alipay_out_biz_no(order.id))
            return _apply_alipay_status(order, payout_method, payload)
        if provider == "wechat":
            payload = wechat_payout_service.query_transfer(wechat_out_batch_no(order.id))
            return _apply_wechat_status(order, payout_method, payload)
    except Exception as exc:
        return _set_failed(order, f"{provider.upper()}_PAYOUT_SYNC_FAILED", str(exc))
    return _set_blocked(order, "PAYOUT_PROVIDER_UNKNOWN", "Unknown payout provider")


def _release_stripe_payout(db: Session, order: Order, seller: User, payout_method: PayoutMethod, amount_minor: int) -> PayoutTransition:
    if order.psp != "stripe":
        return _set_blocked(
            order,
            "BANK_PAYOUT_RAIL_UNAVAILABLE",
            "Automatic Australian bank payouts currently require orders settled on Stripe",
        )
    if not settings.stripe_enabled:
        return _set_blocked(order, "PROVIDER_NOT_READY", "Stripe payouts are not configured on the platform")
    if not seller.stripe_connect_id:
        return _set_blocked(order, "SELLER_NOT_ONBOARDED", "Seller has not completed Stripe Connect onboarding")
    if not payout_method.payouts_enabled:
        return _set_blocked(order, "BANK_PAYOUT_NOT_READY", "Seller does not have an enabled bank payout destination")

    try:
        source_reference = _stripe_source_reference(order)
        source_transaction = stripe_service.resolve_transfer_source_transaction(source_reference) if source_reference else None
        transfer = stripe_service.create_transfer(
            amount_minor=amount_minor,
            currency=(order.charge_currency or "aud").lower(),
            destination_account_id=seller.stripe_connect_id,
            source_transaction=source_transaction,
            transfer_group=payout_transfer_group(order.id),
            metadata={
                "order_id": str(order.id),
                "seller_id": order.seller_id,
                "buyer_id": order.buyer_id,
            },
        )
    except Exception as exc:
        return _set_failed(order, "STRIPE_TRANSFER_FAILED", str(exc))
    return _set_released(order, "stripe", payout_method, transfer.get("id"))


def _release_paypal_payout(order: Order, payout_method: PayoutMethod, amount_minor: int) -> PayoutTransition:
    if not settings.paypal_payout_enabled:
        return _set_blocked(order, "PROVIDER_NOT_READY", "PayPal payouts are not configured on the platform")
    if not payout_method.payouts_enabled:
        return _set_blocked(order, "PAYPAL_PAYOUT_NOT_READY", "Seller does not have an enabled PayPal payout destination")
    # New marketplace orders use PayPal's native delayed-disbursement rail. The
    # capture ID is the reference PayPal releases; no second email payout is sent.
    if getattr(order, "paypal_disbursement_mode", None) == "DELAYED":
        if not order.psp_transaction_id or not payout_method.paypal_merchant_id:
            return _set_blocked(
                order,
                "PAYPAL_DELAYED_DISBURSEMENT_NOT_READY",
                "PayPal delayed-disbursement references are missing",
            )
        try:
            payload = paypal_partner_service.create_referenced_payout(order.psp_transaction_id)
        except Exception as exc:
            return _set_failed(order, "PAYPAL_REFERENCED_PAYOUT_FAILED", str(exc))
        reference = (
            payload.get("payout_item_id")
            or payload.get("transaction_id")
            or payload.get("id")
            or order.psp_transaction_id
        )
        return _set_released(order, "paypal", payout_method, reference)

    # Legacy compatibility for orders captured before native delayed disbursement.
    receiver = (payout_method.account_ref or "").strip().lower()
    if not receiver:
        return _set_blocked(order, "PAYPAL_PAYOUT_NOT_READY", "Seller does not have an enabled PayPal payout destination")
    try:
        payload = paypal_payout_service.create_payout(
            sender_batch_id=paypal_sender_batch_id(order.id),
            sender_item_id=f"order-{order.id}-seller-{order.seller_id}",
            receiver=receiver,
            amount_minor=amount_minor,
            currency=(order.charge_currency or "aud").lower(),
            note=f"HeyMarket payout for order #{order.id}",
            email_subject="Your HeyMarket payout is on the way",
        )
    except Exception as exc:
        return _set_failed(order, "PAYPAL_PAYOUT_CREATE_FAILED", str(exc))
    return _apply_paypal_batch_status(order, payout_method, payload)


def _release_alipay_payout(order: Order, payout_method: PayoutMethod, amount_minor: int) -> PayoutTransition:
    if not settings.alipay_payout_enabled:
        return _set_blocked(order, "PROVIDER_NOT_READY", "Alipay payouts are not configured on the platform")
    payee_account = (payout_method.account_ref or "").strip()
    if not payout_method.payouts_enabled or not payee_account:
        return _set_blocked(order, "ALIPAY_PAYOUT_NOT_READY", "Seller does not have an enabled Alipay payout destination")
    currency_error = _require_cny(order, "Alipay")
    if currency_error:
        return _set_blocked(order, "ALIPAY_CURRENCY_NOT_SUPPORTED", currency_error)
    try:
        payload = alipay_payout_service.create_transfer(
            out_biz_no=alipay_out_biz_no(order.id),
            payee_account=payee_account,
            amount_minor=amount_minor,
            currency=(order.charge_currency or "cny").lower(),
            remark=f"HeyMarket payout for order #{order.id}",
        )
    except Exception as exc:
        return _set_failed(order, "ALIPAY_PAYOUT_CREATE_FAILED", str(exc))
    return _apply_alipay_status(order, payout_method, payload)


def _release_wechat_payout(order: Order, payout_method: PayoutMethod, amount_minor: int) -> PayoutTransition:
    if not settings.wechat_payout_enabled:
        return _set_blocked(order, "PROVIDER_NOT_READY", "WeChat payouts are not configured on the platform")
    openid = (payout_method.account_ref or "").strip()
    if not payout_method.payouts_enabled or not openid:
        return _set_blocked(order, "WECHAT_PAYOUT_NOT_READY", "Seller does not have an enabled WeChat payout destination")
    currency_error = _require_cny(order, "WeChat")
    if currency_error:
        return _set_blocked(order, "WECHAT_CURRENCY_NOT_SUPPORTED", currency_error)
    try:
        payload = wechat_payout_service.create_transfer(
            out_batch_no=wechat_out_batch_no(order.id),
            openid=openid,
            amount_minor=amount_minor,
            currency=(order.charge_currency or "cny").lower(),
            remark=f"Order {order.id} payout",
        )
    except Exception as exc:
        return _set_failed(order, "WECHAT_PAYOUT_CREATE_FAILED", str(exc))
    return _apply_wechat_status(order, payout_method, payload)


def release_payout_for_order(db: Session, order: Order) -> PayoutTransition:
    """Attempt to release seller funds to the seller's configured payout destination."""
    if order.payout_provider in ASYNC_PAYOUT_PROVIDERS and order.payout_status == PAYOUT_PROCESSING:
        payout_method = _preferred_method(db, order.seller_id, order.payout_provider)
        if not payout_method:
            return _set_blocked(order, "PAYOUT_METHOD_MISSING", "Seller no longer has the payout method required to finish this payout")
        return _sync_provider_payout(order, payout_method)
    if order.payout_status == PAYOUT_RELEASED and order.payout_reference:
        return PayoutTransition(status=PAYOUT_RELEASED, reference=order.payout_reference)
    if order.payout_paused:
        return _set_blocked(order, "PAYOUT_PAUSED", "Payout is paused on this order")
    if order.dispute_status in {"open", "refund_requested"}:
        return _set_blocked(order, "DISPUTE_OPEN", "Payout is blocked while the dispute is open")
    if order.status not in ELIGIBLE_RELEASE_STATUSES:
        return _set_blocked(order, "ORDER_NOT_COMPLETED", "Payout can release only after order completion")
    if (order.payment_status or "").lower() not in SUCCESSFUL_PAYMENT_STATUSES:
        return _set_blocked(order, "PAYMENT_NOT_SETTLED", "Buyer payment is not settled yet")

    seller = db.query(User).filter(User.id == order.seller_id).first()
    if not seller:
        return _set_blocked(order, "SELLER_NOT_FOUND", "Seller account was not found")

    payout_method = _preferred_release_method(db, order.seller_id, order.psp)
    if not payout_method:
        provider = (order.psp or "").strip().lower()
        if provider:
            return _set_blocked(
                order,
                "PAYOUT_METHOD_MISSING",
                f"Seller has not configured a payout destination for {provider}",
            )
        return _set_blocked(order, "PAYOUT_METHOD_MISSING", "Seller has not configured any payout destination")

    amount_minor = amount_to_minor(order.amount or 0.0)
    if amount_minor <= 0:
        return _set_blocked(order, "INVALID_PAYOUT_AMOUNT", "Order amount is invalid for payout release")

    if payout_method.type == "bank":
        return _release_stripe_payout(db, order, seller, payout_method, amount_minor)
    if payout_method.type == "paypal":
        return _release_paypal_payout(order, payout_method, amount_minor)
    if payout_method.type == "alipay":
        return _release_alipay_payout(order, payout_method, amount_minor)
    if payout_method.type == "wechat":
        return _release_wechat_payout(order, payout_method, amount_minor)

    return _set_blocked(order, "PAYOUT_METHOD_UNSUPPORTED", "Seller payout method is not supported")


def reverse_released_payout_for_order(order: Order) -> PayoutTransition:
    if order.payout_status != PAYOUT_RELEASED or not order.payout_reference:
        return PayoutTransition(status=order.payout_status or PAYOUT_PENDING)
    if order.payout_provider != "stripe":
        return _set_blocked(
            order,
            "REVERSAL_NOT_SUPPORTED",
            "Automatic payout reversal is currently implemented only for Stripe releases",
        )
    if not settings.stripe_enabled:
        return _set_failed(order, "PROVIDER_NOT_READY", "Stripe payouts are not configured on the platform")
    try:
        reversal = stripe_service.create_transfer_reversal(
            transfer_id=order.payout_reference,
            amount_minor=amount_to_minor(order.amount or 0.0),
            metadata={"order_id": str(order.id)},
        )
    except Exception as exc:
        return _set_failed(order, "STRIPE_REVERSAL_FAILED", str(exc))

    order.payout_status = PAYOUT_REVERSED
    order.payout_failure_code = None
    order.payout_failure_reason = None
    order.payout_failed_at = None
    order.payout_reversed_at = _now()
    order.payout_reversal_reference = reversal.get("id")
    return PayoutTransition(status=PAYOUT_REVERSED, changed=True, reference=order.payout_reversal_reference)
