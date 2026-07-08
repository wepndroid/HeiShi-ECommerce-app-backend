"""Mark orders paid after PSP confirmation."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session, joinedload

from app.catalog_helpers import (
    apply_bundle_item_payment,
    apply_bundle_payment,
    listing_checkout_amount,
)
from app.models import Listing, Order, User
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
    if listing:
        if order.bundle_item_id:
            from app.catalog_helpers import bundle_item_separate_price, find_bundle_item

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
    _mark_coupon_used(db, order.coupon_id)
    if listing and listing.type == "service":
        order.status = "pendingService"
        schedule_auto_confirm(order)
    else:
        order.status = "pendingShip"
    order.payment_status = "succeeded"
    order.updated_at = datetime.now(timezone.utc)
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
    if not paid_order or not paid_order.listing:
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
