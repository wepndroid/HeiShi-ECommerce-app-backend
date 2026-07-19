from datetime import datetime, timezone
import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.auth import get_accept_language, get_current_user
from app.admin_notifications import notify_admin
from app.blocklist_helpers import users_blocked
from app.catalog_helpers import (
    apply_bundle_item_payment,
    apply_bundle_payment,
    bundle_allows_separate_sale,
    bundle_item_is_available,
    bundle_item_separate_price,
    expire_stale_pending_pay_orders,
    find_bundle_item,
    listing_checkout_amount,
    release_order_bundle_hold,
)
from app.config import settings
from app.coupon_service import refresh_expired_coupons
from app.database import SessionLocal, get_db
from app.models import Coupon, Listing, Order, Review, User
from app.order_jobs import schedule_auto_confirm
from app.notification_jobs import enqueue_notification
from app.payments.fulfillment import dispatch_order_paid_push, fulfill_paid_order
from app.pagination import paginate
from app.platform_config import escrow_fee_from_db
from app.payout_release import release_payout_for_order
from app.push_notifications import send_order_paid_push, send_order_remind_push
from app.schemas import (
    CreateOrderRequest,
    OrderDto,
    OrderReviewDto,
    Paginated,
    ReviewCriteriaDto,
    ReviewRequest,
    UpdateOrderRequest,
)
from app.serializers import iso, order_to_dto

router = APIRouter(prefix="/orders", tags=["orders"])

OPEN_ORDER_STATUSES = ("pendingPay", "pendingShip", "pendingService", "pendingReceive", "pendingReview")
INVENTORY_OWNING_ORDER_STATUSES = ("pendingShip", "pendingService", "pendingReceive", "pendingReview")


def _set_display_amount_cny(order: Order) -> None:
    if order.amount and order.amount > 0:
        order.display_amount_cny = round(float(order.amount) * settings.aud_to_cny_display_rate, 2)


def _criteria_overall_rating(criteria: ReviewCriteriaDto) -> int:
    values = [
        criteria.quality,
        criteria.communication,
        criteria.trustement,
    ]
    return round(sum(values) / len(values))


def _review_criteria_from_model(review: Review) -> ReviewCriteriaDto | None:
    if review.quality_rating is None:
        return None
    return ReviewCriteriaDto(
        quality=review.quality_rating,
        communication=review.communication_rating or review.quality_rating,
        trustement=review.expertise_rating or review.quality_rating,
    )


def _resolve_review_payload(body: ReviewRequest) -> tuple[int, ReviewCriteriaDto | None, str | None]:
    if body.criteria is not None:
        comment = (body.comment or "").strip()
        if not comment:
            raise HTTPException(
                status_code=400,
                detail={"code": "VALIDATION_ERROR", "message": "Comment is required", "details": {}},
            )
        return _criteria_overall_rating(body.criteria), body.criteria, comment
    if body.rating is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": "criteria or rating is required", "details": {}},
        )
    return body.rating, None, body.comment


def _order_review_dto(review: Review) -> OrderReviewDto:
    criteria = _review_criteria_from_model(review)
    return OrderReviewDto(
        rating=review.rating,
        comment=review.comment,
        criteria=criteria,
        createdAt=iso(review.created_at),
    )


def _apply_coupon(
    db: Session,
    user_id: str,
    coupon_id: str | None,
    base_amount: float,
    *,
    exclude_order_id: int | None = None,
) -> tuple[float, float, str | None]:
    if not coupon_id:
        return round(base_amount, 2), 0.0, None
    refresh_expired_coupons(db, user_id)
    pending = db.query(Order).filter(
        Order.buyer_id == user_id,
        Order.coupon_id == coupon_id,
        Order.status == "pendingPay",
    )
    if exclude_order_id is not None:
        pending = pending.filter(Order.id != exclude_order_id)
    if pending.first():
        raise HTTPException(
            status_code=409,
            detail={"code": "COUPON_IN_USE", "message": "Coupon already applied to another unpaid order", "details": {}},
        )
    coupon = db.query(Coupon).filter(Coupon.id == coupon_id, Coupon.user_id == user_id).first()
    if not coupon:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Coupon not found", "details": {}})
    if coupon.status != "available":
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATE", "message": "Coupon not available", "details": {}})
    if coupon.expires_at and coupon.expires_at < datetime.now(timezone.utc):
        coupon.status = "expired"
        db.commit()
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATE", "message": "Coupon expired", "details": {}})
    discount = min(float(coupon.amount), float(base_amount))
    return round(base_amount - discount, 2), round(discount, 2), coupon_id


def _mark_coupon_used(db: Session, coupon_id: str | None) -> None:
    if not coupon_id:
        return
    coupon = db.query(Coupon).filter(Coupon.id == coupon_id).first()
    if coupon and coupon.status == "available":
        coupon.status = "used"


def _conflicting_open_order(
    db: Session,
    listing_id: int,
    bundle_item_id: str | None,
) -> Order | None:
    open_orders = (
        db.query(Order)
        .filter(Order.listing_id == listing_id, Order.status.in_(INVENTORY_OWNING_ORDER_STATUSES))
        .all()
    )
    for order in open_orders:
        if order.bundle_item_id is None:
            return order
        if bundle_item_id is None:
            return order
        if order.bundle_item_id == bundle_item_id:
            return order
    return None


def _bundle_has_on_hold_items(listing: Listing) -> bool:
    if listing.type != "bundle" or not isinstance(listing.bundle_meta, dict):
        return False
    return any(
        isinstance(item, dict) and item.get("status") == "onHold"
        for item in (listing.bundle_meta.get("items") or [])
    )


def _reviewed_order_ids(db: Session, user_id: str, orders: list[Order]) -> set[int]:
    order_ids = [order.id for order in orders]
    if not order_ids:
        return set()
    return {
        order_id
        for (order_id,) in (
            db.query(Review.order_id)
            .filter(Review.reviewer_id == user_id, Review.order_id.in_(order_ids))
            .all()
        )
    }


@router.get("", response_model=Paginated[OrderDto])
def list_orders(
    request: Request,
    status: str | None = None,
    listingId: int | None = None,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lang = get_accept_language(request)
    expire_stale_pending_pay_orders(db, settings.pending_pay_expire_minutes)
    q = (
        db.query(Order)
        .options(joinedload(Order.listing), joinedload(Order.seller))
        .filter(Order.buyer_id == user.id)
    )
    if status and status != "all":
        q = q.filter(Order.status == status)
    if listingId is not None:
        q = q.filter(Order.listing_id == listingId)
    q = q.order_by(Order.created_at.desc())
    total = q.count()
    items = q.offset((page - 1) * pageSize).limit(pageSize).all()
    reviewed_ids = _reviewed_order_ids(db, user.id, items)
    return paginate(
        [order_to_dto(o, lang, viewer_has_reviewed=o.id in reviewed_ids) for o in items],
        page,
        pageSize,
        total,
    )


@router.get("/sales", response_model=Paginated[OrderDto])
def list_sales(
    request: Request,
    status: str | None = None,
    listingId: int | None = None,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lang = get_accept_language(request)
    expire_stale_pending_pay_orders(db, settings.pending_pay_expire_minutes)
    q = (
        db.query(Order)
        .options(joinedload(Order.listing), joinedload(Order.seller), joinedload(Order.buyer))
        .filter(Order.seller_id == user.id)
    )
    if status and status != "all":
        q = q.filter(Order.status == status)
    if listingId is not None:
        q = q.filter(Order.listing_id == listingId)
    q = q.order_by(Order.updated_at.desc())
    total = q.count()
    items = q.offset((page - 1) * pageSize).limit(pageSize).all()
    reviewed_ids = _reviewed_order_ids(db, user.id, items)
    return paginate(
        [
            order_to_dto(
                o,
                lang,
                include_buyer=True,
                viewer_has_reviewed=o.id in reviewed_ids,
            )
            for o in items
        ],
        page,
        pageSize,
        total,
    )


@router.get("/{order_id}", response_model=OrderDto)
def get_order(order_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = _get_participant_order(db, order_id, user.id)
    include_buyer = order.seller_id == user.id
    return order_to_dto(
        order,
        get_accept_language(request),
        include_buyer=include_buyer,
        viewer_has_reviewed=order.id in _reviewed_order_ids(db, user.id, [order]),
    )


@router.post("", response_model=OrderDto, status_code=201)
def create_order(
    body: CreateOrderRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    listing = (
        db.query(Listing)
        .options(joinedload(Listing.seller))
        .filter(Listing.id == body.listingId)
        .with_for_update()
        .first()
    )
    expire_stale_pending_pay_orders(db, settings.pending_pay_expire_minutes)
    if not listing or listing.status != "active":
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Listing not available", "details": {}})
    if listing.seller_id == user.id:
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATE", "message": "Cannot buy own listing", "details": {}})
    if users_blocked(db, user.id, listing.seller_id):
        raise HTTPException(
            status_code=403,
            detail={"code": "USER_BLOCKED", "message": "You cannot trade with this user", "details": {}},
        )
    existing = _conflicting_open_order(db, listing.id, body.bundleItemId)
    if existing:
        code = "LISTING_RESERVED" if existing.buyer_id == user.id else "LISTING_RESERVED_BY_OTHER"
        raise HTTPException(
            status_code=409,
            detail={"code": code, "message": "This listing already has an active order", "details": {}},
        )
    bundle_item_id: str | None = None
    if body.bundleItemId:
        if listing.type != "bundle":
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_STATE", "message": "This listing does not support separate purchase", "details": {}},
            )
        if not bundle_allows_separate_sale(listing):
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_STATE", "message": "Separate purchase is not allowed for this bundle", "details": {}},
            )
        item = find_bundle_item(listing, body.bundleItemId)
        if not item or not bundle_item_is_available(item):
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_STATE", "message": "Bundle item is not available", "details": {}},
            )
        checkout_amount = bundle_item_separate_price(item)
        if checkout_amount <= 0:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_STATE", "message": "Bundle item has no separate price", "details": {}},
            )
        bundle_item_id = body.bundleItemId
    else:
        if listing.type == "bundle" and _bundle_has_on_hold_items(listing):
            raise HTTPException(
                status_code=409,
                detail={"code": "LISTING_RESERVED_BY_OTHER", "message": "This listing already has an active order", "details": {}},
            )
        checkout_amount = listing_checkout_amount(listing)
        if checkout_amount <= 0:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_STATE", "message": "Listing is no longer available for purchase", "details": {}},
            )
    payable, discount, coupon_id = _apply_coupon(db, user.id, body.couponId, checkout_amount)
    escrow_fee = escrow_fee_from_db(db) if listing.escrow_supported else 0.0
    order = Order(
        buyer_id=user.id,
        listing_id=listing.id,
        seller_id=listing.seller_id,
        status="pendingPay",
        amount=payable,
        escrow_fee=escrow_fee,
        delivery_method=body.deliveryMethod,
        payment_method_id=body.paymentMethodId,
        bundle_item_id=bundle_item_id,
        coupon_id=coupon_id,
        discount_amount=discount,
    )
    _set_display_amount_cny(order)
    db.add(order)
    db.commit()
    db.refresh(order)
    order.listing = listing
    order.seller = listing.seller
    return order_to_dto(order, get_accept_language(request))


@router.patch("/{order_id}", response_model=OrderDto)
def update_order(
    order_id: int,
    body: UpdateOrderRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = _get_buyer_order(db, order_id, user.id)
    if order.status != "pendingPay":
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_STATE", "message": "Only unpaid orders can be updated", "details": {}},
        )
    if body.deliveryMethod is not None:
        order.delivery_method = body.deliveryMethod
    if body.paymentMethodId is not None:
        order.payment_method_id = body.paymentMethodId
    patch = body.model_dump(exclude_unset=True)
    listing = db.query(Listing).filter(Listing.id == order.listing_id).first()
    if listing:
        if order.bundle_item_id:
            item = find_bundle_item(listing, order.bundle_item_id)
            base = bundle_item_separate_price(item) if item else order.amount + order.discount_amount
        else:
            base = listing_checkout_amount(listing)
        order.escrow_fee = escrow_fee_from_db(db) if listing.escrow_supported else 0.0
        coupon_id = patch["couponId"] if "couponId" in patch else order.coupon_id
        payable, discount, resolved_coupon = _apply_coupon(
            db, user.id, coupon_id, base, exclude_order_id=order.id
        )
        order.amount = payable
        order.discount_amount = discount
        order.coupon_id = resolved_coupon
    order.updated_at = datetime.now(timezone.utc)
    _set_display_amount_cny(order)
    db.commit()
    db.refresh(order)
    return order_to_dto(order, get_accept_language(request))


class SellerAdjustAmountRequest(BaseModel):
    amount: float = Field(gt=0)


@router.post("/{order_id}/seller-adjust-amount", response_model=OrderDto)
def seller_adjust_amount(
    order_id: int,
    body: SellerAdjustAmountRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = _get_seller_order(db, order_id, user.id)
    if order.status != "pendingPay":
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_STATE", "message": "Only unpaid orders can be repriced", "details": {}},
        )
    order.amount = round(body.amount, 2)
    _set_display_amount_cny(order)
    order.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(order)
    return order_to_dto(order, get_accept_language(request), include_buyer=True)


@router.post("/{order_id}/pay", response_model=OrderDto)
def pay_order(
    order_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = _get_buyer_order(db, order_id, user.id)
    if order.status != "pendingPay":
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATE", "message": "Order is not pending payment", "details": {}})
    # Real (non-simulated) payments must be confirmed by the PSP first — the mobile runs
    # /payments/checkout (PaymentIntent off the saved card, or a hosted session) and the
    # webhook / confirm step sets payment_status before pay finalises the order.
    if not settings.payments_simulated and order.payment_status not in ("succeeded", "paid"):
        raise HTTPException(
            status_code=400,
            detail={"code": "PAYMENT_PENDING", "message": "Payment has not been confirmed yet", "details": {}},
        )
    listing = (
        db.query(Listing)
        .filter(Listing.id == order.listing_id)
        .with_for_update()
        .first()
    )
    if listing:
        if order.bundle_item_id:
            item = find_bundle_item(listing, order.bundle_item_id)
            expected = bundle_item_separate_price(item) if item else 0.0
        else:
            expected = listing_checkout_amount(listing)
        if expected <= 0:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_STATE", "message": "Listing is no longer available for purchase", "details": {}},
            )
        payable, discount, _ = _apply_coupon(
            db, user.id, order.coupon_id, expected, exclude_order_id=order.id
        )
        order.amount = payable
        order.discount_amount = discount
        order.escrow_fee = escrow_fee_from_db(db) if listing.escrow_supported else 0.0
    fulfill_paid_order(db, order)
    db.refresh(order)
    paid_order = (
        db.query(Order)
        .options(joinedload(Order.listing), joinedload(Order.buyer))
        .filter(Order.id == order_id)
        .first()
    )
    if paid_order and paid_order.listing:
        buyer_name = paid_order.buyer.nickname if paid_order.buyer else "Buyer"
        background_tasks.add_task(
            _dispatch_order_paid_push,
            paid_order.seller_id,
            buyer_name,
            paid_order.id,
            paid_order.listing.title,
        )
    return order_to_dto(paid_order or order, get_accept_language(request))


@router.post("/{order_id}/ship", response_model=OrderDto)
def ship_order(order_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = _get_seller_order(db, order_id, user.id)
    if order.status != "pendingShip":
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATE", "message": "Order is not pending ship", "details": {}})
    order.status = "pendingReceive"
    schedule_auto_confirm(order)
    order.updated_at = datetime.now(timezone.utc)
    enqueue_notification(
        db,
        user_id=order.buyer_id,
        role="buyer",
        category="delivery_update",
        notification_type="seller_shipped_order",
        title="Seller confirmed shipment",
        body=f"Order #{order.id} is on its way.",
        title_zh="卖家已发货",
        body_zh=f"订单 #{order.id} 已发货。",
        business_type="order",
        business_id=str(order.id),
        deep_link=f"heymarket://order/{order.id}",
        deduplication_key=f"order:{order.id}:shipped",
        mandatory=True,
    )
    db.commit()
    db.refresh(order)
    return order_to_dto(order, get_accept_language(request), include_buyer=True)


@router.post("/{order_id}/complete-service", response_model=OrderDto)
def complete_service_order(
    order_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    order = _get_seller_order(db, order_id, user.id)
    if order.status != "pendingService":
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_STATE", "message": "Order is not pending service", "details": {}},
        )
    order.status = "pendingReceive"
    schedule_auto_confirm(order)
    order.updated_at = datetime.now(timezone.utc)
    enqueue_notification(
        db,
        user_id=order.buyer_id,
        role="buyer",
        category="delivery_update",
        notification_type="seller_completed_service",
        title="Service marked complete",
        body=f"Please review and confirm order #{order.id}.",
        title_zh="卖家已完成服务",
        body_zh=f"请检查并确认订单 #{order.id}。",
        business_type="order",
        business_id=str(order.id),
        deep_link=f"heymarket://order/{order.id}",
        deduplication_key=f"order:{order.id}:service-complete",
        mandatory=True,
    )
    db.commit()
    db.refresh(order)
    return order_to_dto(order, get_accept_language(request), include_buyer=True)


def _dispatch_order_paid_push(
    seller_id: str,
    buyer_name: str,
    order_id: int,
    listing_title: str,
) -> None:
    db = SessionLocal()
    try:
        seller = db.query(User).filter(User.id == seller_id).first()
        lang = seller.language if seller and seller.language else "en"
        send_order_paid_push(
            db,
            seller_id=seller_id,
            buyer_name=buyer_name,
            order_id=order_id,
            listing_title=listing_title,
            lang=lang,
        )
    finally:
        db.close()


def _dispatch_remind_ship_push(
    seller_id: str,
    buyer_name: str,
    order_id: int,
    listing_title: str,
) -> None:
    db = SessionLocal()
    try:
        seller = db.query(User).filter(User.id == seller_id).first()
        lang = seller.language if seller and seller.language else "en"
        send_order_remind_push(
            db,
            seller_id=seller_id,
            buyer_name=buyer_name,
            order_id=order_id,
            listing_title=listing_title,
            lang=lang,
        )
    finally:
        db.close()


@router.post("/{order_id}/remind-ship", status_code=204)
def remind_ship(
    order_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = (
        db.query(Order)
        .options(joinedload(Order.listing))
        .filter(Order.id == order_id, Order.buyer_id == user.id)
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Order not found", "details": {}})
    if order.status not in ("pendingShip", "pendingReceive"):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_STATE", "message": "Cannot remind ship for this order", "details": {}},
        )
    listing_title = order.listing.title if order.listing else f"Order #{order_id}"
    background_tasks.add_task(
        _dispatch_remind_ship_push,
        order.seller_id,
        user.nickname,
        order.id,
        listing_title,
    )
    return Response(status_code=204)


@router.post("/{order_id}/confirm-receive", response_model=OrderDto)
def confirm_receive(order_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = _get_buyer_order(db, order_id, user.id)
    if order.status not in ("pendingReceive", "pendingService"):
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATE", "message": "Order is not pending confirmation", "details": {}})
    order.status = "pendingReview"
    release_payout_for_order(db, order)
    order.confirmed_at = datetime.now(timezone.utc)
    order.auto_confirm_at = None
    order.updated_at = datetime.now(timezone.utc)
    enqueue_notification(
        db,
        user_id=order.seller_id,
        role="seller",
        category="payout",
        notification_type="buyer_confirmed_receipt",
        title="Buyer confirmed receipt",
        body=f"Order #{order.id} was confirmed and seller settlement was initiated.",
        title_zh="买家已确认收货",
        body_zh=f"订单 #{order.id} 已确认，卖家结算已开始。",
        business_type="order",
        business_id=str(order.id),
        deep_link=f"heymarket://order/{order.id}",
        deduplication_key=f"order:{order.id}:receipt-confirmed:seller",
        mandatory=True,
    )
    db.commit()
    db.refresh(order)
    return order_to_dto(order, get_accept_language(request))


class OrderReasonRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    evidenceUrls: list[str] = Field(default_factory=list)


def _open_refund_style_dispute(
    order: Order,
    *,
    reason: str,
    evidence_urls: list[str],
) -> None:
    order.status = "refundInProgress"
    order.payout_paused = True
    order.dispute_status = "refund_requested"
    order.dispute_reason = reason
    order.dispute_evidence_json = json.dumps(evidence_urls[:10])
    order.updated_at = datetime.now(timezone.utc)


@router.post("/{order_id}/refund", response_model=OrderDto)
def request_refund(
    order_id: int,
    body: OrderReasonRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = _get_buyer_order(db, order_id, user.id)
    if order.status not in ("pendingShip", "pendingService", "pendingReceive", "completed"):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_STATE", "message": "Refund cannot be requested for this order", "details": {}},
        )
    _open_refund_style_dispute(order, reason=body.reason, evidence_urls=body.evidenceUrls)
    enqueue_notification(
        db,
        user_id=order.seller_id,
        role="seller",
        category="refund_update",
        notification_type="buyer_requested_refund",
        title="Buyer requested a refund",
        body=f"Order #{order.id} is awaiting dispute review.",
        title_zh="买家申请退款",
        body_zh=f"订单 #{order.id} 正在等待平台审核。",
        business_type="order",
        business_id=str(order.id),
        deep_link=f"heymarket://order/{order.id}",
        deduplication_key=f"order:{order.id}:refund-requested:seller",
        mandatory=True,
    )
    notify_admin(
        db,
        event_type="refund_requested",
        title="New refund request",
        body=f"{user.nickname} requested a refund for order #{order.id}.",
        target_type="order",
        target_id=order.id,
        action_path=f"/orders/{order.id}",
    )
    db.commit()
    db.refresh(order)
    return order_to_dto(order, get_accept_language(request))


class OpenDisputeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    evidenceUrls: list[str] = Field(default_factory=list)


@router.post("/{order_id}/dispute", response_model=OrderDto)
def open_dispute(
    order_id: int,
    body: OpenDisputeRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = _get_buyer_order(db, order_id, user.id)
    if order.status not in ("pendingShip", "pendingService", "pendingReceive", "completed"):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_STATE", "message": "Order cannot enter dispute in current state", "details": {}},
        )
    _open_refund_style_dispute(order, reason=body.reason, evidence_urls=body.evidenceUrls)
    enqueue_notification(
        db,
        user_id=order.seller_id,
        role="seller",
        category="dispute",
        notification_type="buyer_opened_dispute",
        title="Buyer opened a dispute",
        body=f"Order #{order.id} requires your attention.",
        title_zh="买家发起争议",
        body_zh=f"订单 #{order.id} 需要您的处理。",
        business_type="order",
        business_id=str(order.id),
        deep_link=f"heymarket://order/{order.id}",
        deduplication_key=f"order:{order.id}:dispute-opened:seller",
        mandatory=True,
    )
    notify_admin(
        db,
        event_type="dispute_opened",
        title="New order dispute",
        body=f"{user.nickname} opened a dispute for order #{order.id}.",
        target_type="order",
        target_id=order.id,
        action_path=f"/orders/{order.id}",
    )
    db.commit()
    db.refresh(order)
    return order_to_dto(order, get_accept_language(request))


@router.post("/{order_id}/cancel", response_model=OrderDto)
def cancel_order(order_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = _get_buyer_order(db, order_id, user.id)
    if order.status != "pendingPay":
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_STATE", "message": "Only unpaid orders can be cancelled", "details": {}},
        )
    order.status = "cancelled"
    order.updated_at = datetime.now(timezone.utc)
    order.coupon_id = None
    order.discount_amount = 0.0
    release_order_bundle_hold(db, order)
    db.commit()
    db.refresh(order)
    return order_to_dto(order, get_accept_language(request))


@router.post("/{order_id}/seller-cancel", response_model=OrderDto)
def seller_cancel_order(
    order_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = _get_seller_order(db, order_id, user.id)
    if order.status != "pendingPay":
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_STATE", "message": "Only unpaid orders can be released", "details": {}},
        )
    order.status = "cancelled"
    order.updated_at = datetime.now(timezone.utc)
    order.coupon_id = None
    order.discount_amount = 0.0
    release_order_bundle_hold(db, order)
    db.commit()
    db.refresh(order)
    return order_to_dto(order, get_accept_language(request), include_buyer=True)


@router.get("/{order_id}/review", response_model=OrderReviewDto)
def get_order_review(
    order_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = _get_participant_order(db, order_id, user.id)
    review = (
        db.query(Review)
        .filter(Review.order_id == order.id, Review.reviewer_id == user.id)
        .first()
    )
    if not review:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "Review not found", "details": {}},
        )
    return _order_review_dto(review)


@router.post("/{order_id}/review", status_code=204)
def submit_review(
    order_id: int,
    body: ReviewRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = _get_participant_order(db, order_id, user.id)
    if order.status not in ("pendingReview", "completed", "refunded"):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_STATE", "message": "Order is not open for review", "details": {}},
        )
    if (
        db.query(Review)
        .filter(Review.order_id == order.id, Review.reviewer_id == user.id)
        .first()
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "ALREADY_EXISTS", "message": "Review already submitted", "details": {}},
        )
    rating, criteria, comment = _resolve_review_payload(body)
    review = Review(
        order_id=order.id,
        reviewer_id=user.id,
        rating=rating,
        comment=comment,
    )
    if criteria is not None:
        review.quality_rating = criteria.quality
        review.communication_rating = criteria.communication
        review.expertise_rating = criteria.trustement
    db.add(review)
    db.flush()
    notify_admin(
        db,
        event_type="review_submitted",
        title="New review submitted",
        body=f"{user.nickname} reviewed order #{order.id}.",
        target_type="review",
        target_id=review.id,
        action_path=f"/reviews/{review.id}",
    )
    _maybe_complete_order_after_review(db, order)
    db.commit()
    return Response(status_code=204)


def _maybe_complete_order_after_review(db: Session, order: Order) -> None:
    review_count = db.query(Review).filter(Review.order_id == order.id).count()
    if review_count < 2 or order.status != "pendingReview":
        return
    order.status = "completed"
    release_payout_for_order(db, order)
    order.updated_at = datetime.now(timezone.utc)
    listing = db.query(Listing).filter(Listing.id == order.listing_id).first()
    if listing and listing.status != "sold":
        listing.status = "sold"


def _get_participant_order(db: Session, order_id: int, user_id: str) -> Order:
    order = (
        db.query(Order)
        .options(joinedload(Order.listing), joinedload(Order.seller), joinedload(Order.buyer))
        .filter(
            Order.id == order_id,
            or_(Order.buyer_id == user_id, Order.seller_id == user_id),
        )
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Order not found", "details": {}})
    return order


def _get_buyer_order(db: Session, order_id: int, buyer_id: str) -> Order:
    order = (
        db.query(Order)
        .options(joinedload(Order.listing), joinedload(Order.seller))
        .filter(Order.id == order_id, Order.buyer_id == buyer_id)
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Order not found", "details": {}})
    return order


def _get_seller_order(db: Session, order_id: int, seller_id: str) -> Order:
    order = (
        db.query(Order)
        .options(joinedload(Order.listing), joinedload(Order.seller), joinedload(Order.buyer))
        .filter(Order.id == order_id, Order.seller_id == seller_id)
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Order not found", "details": {}})
    return order
