"""Mark orders paid after PSP confirmation."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session, joinedload

from app.catalog_helpers import (
    apply_bundle_item_payment,
    apply_bundle_payment,
    bundle_item_is_available,
    bundle_item_separate_price,
    find_bundle_item,
    invalidate_other_private_offers,
    listing_checkout_amount,
)
from app.models import Listing, Order, User
from app.notification_jobs import enqueue_notification
from app.order_jobs import schedule_auto_confirm
from app.platform_config import escrow_fee_from_db
from app.push_notifications import send_order_paid_push


def _mark_coupon_used(db: Session, coupon_id: str | None) -> None:
    if not coupon_id:
        return
    from app.models import Coupon

    coupon = db.query(Coupon).filter(Coupon.id == coupon_id).first()
    if coupon and coupon.status == "available":
        coupon.status = "used"


def _paid_order_inventory_is_available(listing: Listing | None, order: Order) -> bool:
    """Revalidate inventory while holding the listing row lock after PSP success."""
    if not listing or listing.status != "active":
        return False
    if order.bundle_item_id:
        item = find_bundle_item(listing, order.bundle_item_id)
        return bool(item and bundle_item_is_available(item))
    if listing.type == "bundle" and not order.private_offer_id:
        # A full-bundle checkout is priced from the remaining available items. If
        # another buyer completed an item purchase while this buyer was at the PSP,
        # the locked checkout amount no longer represents the remaining bundle.
        locked_base = round(float(order.amount or 0) + float(order.discount_amount or 0), 2)
        return abs(round(listing_checkout_amount(listing), 2) - locked_base) <= 0.01
    return True


def _refund_unavailable_paid_order(
    db: Session,
    order: Order,
    listing: Listing | None,
) -> Order:
    """Compensate a PSP-successful payment that lost the inventory race."""
    from app.payments.refunds import refund_order_payment

    order.payment_status = "succeeded"
    order.payout_paused = True
    order.dispute_reason = (
        "Inventory was purchased by another buyer before this payment completed."
    )
    transition = refund_order_payment(order)
    if transition.status == "refunded":
        order.status = "refunded"
        order.dispute_status = "resolved"
    elif transition.status == "pending":
        order.status = "refundInProgress"
        order.dispute_status = "refund_pending"
    else:
        order.status = "inDispute"
        order.dispute_status = "refund_failed"
    order.updated_at = datetime.now(timezone.utc)
    title = listing.title if listing else f"Order #{order.id}"
    enqueue_notification(
        db,
        user_id=order.buyer_id,
        role="buyer",
        category="payment_update",
        notification_type="inventory_conflict_refund",
        title="Payment is being returned",
        body=(
            f"{title[:120]} was purchased by another buyer before your payment completed. "
            "Your payment has been refunded."
            if transition.status == "refunded"
            else f"{title[:120]} became unavailable. Your payment return is being processed."
        ),
        title_zh="付款正在退回",
        body_zh=(
            "该商品在您的付款完成前已被其他买家购买，款项已退回。"
            if transition.status == "refunded"
            else "该商品已不可用，您的退款正在处理中。"
        ),
        business_type="order",
        business_id=str(order.id),
        deep_link=f"heymarket://order/{order.id}",
        deduplication_key=f"order:{order.id}:inventory-conflict:{transition.status}:buyer",
        mandatory=True,
    )
    enqueue_notification(
        db,
        user_id=order.seller_id,
        role="seller",
        category="payment_update",
        notification_type="duplicate_inventory_payment_blocked",
        title="Duplicate payment blocked",
        body=(
            f"Order #{order.id} will not be fulfilled because the listing was already sold. "
            "The buyer payment is being returned."
        ),
        title_zh="重复付款已拦截",
        body_zh=f"订单 #{order.id} 对应的商品已售出，买家款项正在退回，无需履约。",
        business_type="order",
        business_id=str(order.id),
        deep_link=f"heymarket://order/{order.id}",
        deduplication_key=f"order:{order.id}:inventory-conflict:seller",
        mandatory=True,
    )
    db.commit()
    db.refresh(order)
    return order


def fulfill_paid_order(db: Session, order: Order) -> Order:
    """Transition pendingPay order to pendingShip after successful payment."""
    if order.status != "pendingPay":
        return order
    listing = (
        db.query(Listing)
        .filter(Listing.id == order.listing_id)
        .with_for_update()
        .first()
    )
    if not _paid_order_inventory_is_available(listing, order):
        return _refund_unavailable_paid_order(db, order, listing)
    if listing:
        if order.bundle_item_id:
            item = find_bundle_item(listing, order.bundle_item_id)
            expected = bundle_item_separate_price(item) if item else 0.0
        else:
            expected = listing_checkout_amount(listing)
        if expected > 0:
            order.amount = order.amount or expected
        order.escrow_fee = escrow_fee_from_db(db) if listing.escrow_supported else 0.0
        if listing.status == "active":
            if order.bundle_item_id:
                apply_bundle_item_payment(listing, order.bundle_item_id)
            elif listing.type == "bundle":
                apply_bundle_payment(listing)
            else:
                listing.status = "sold"
        invalidate_other_private_offers(
            db,
            listing_id=listing.id,
            accepted_offer_id=order.private_offer_id,
        )
    _mark_coupon_used(db, order.coupon_id)
    if listing and listing.type == "service":
        order.status = "pendingService"
        schedule_auto_confirm(order)
    else:
        order.status = "pendingShip"
    order.payment_status = "succeeded"
    order.updated_at = datetime.now(timezone.utc)
    title = listing.title if listing else f"Order #{order.id}"
    enqueue_notification(
        db,
        user_id=order.buyer_id,
        role="buyer",
        category="payment_update",
        notification_type="order_payment_succeeded",
        title="Payment successful",
        body=f"Payment for order #{order.id} was successful.",
        title_zh="付款成功",
        body_zh=f"订单 #{order.id} 付款成功。",
        business_type="order",
        business_id=str(order.id),
        deep_link=f"heymarket://order/{order.id}",
        deduplication_key=f"order:{order.id}:payment:succeeded:buyer",
        mandatory=True,
    )
    enqueue_notification(
        db,
        user_id=order.seller_id,
        role="seller",
        category="payment_update",
        notification_type="buyer_payment_succeeded",
        title="Buyer payment received",
        body=f"Order #{order.id} for {title[:120]} is paid and ready for fulfillment.",
        title_zh="买家付款成功",
        body_zh=f"订单 #{order.id} 已付款，请安排交付。",
        business_type="order",
        business_id=str(order.id),
        deep_link=f"heymarket://order/{order.id}",
        deduplication_key=f"order:{order.id}:payment:succeeded:seller",
        mandatory=True,
    )
    db.commit()
    db.refresh(order)
    return order


def dispatch_order_paid_push(db: Session, order_id: int) -> None:
    paid_order = (
        db.query(Order)
        .options(joinedload(Order.listing), joinedload(Order.buyer))
        .filter(Order.id == order_id)
        .first()
    )
    if (
        not paid_order
        or not paid_order.listing
        or paid_order.status not in {"pendingShip", "pendingService"}
    ):
        return
    buyer_name = paid_order.buyer.nickname if paid_order.buyer else "Buyer"
    seller = db.query(User).filter(User.id == paid_order.seller_id).first()
    lang = seller.language if seller and seller.language else "en"
    send_order_paid_push(
        db,
        seller_id=paid_order.seller_id,
        buyer_name=buyer_name,
        order_id=paid_order.id,
        listing_title=paid_order.listing.title,
        lang=lang,
    )
