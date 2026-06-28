from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session, joinedload

from app.auth import get_accept_language, get_current_user
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
    set_bundle_item_status,
)
from app.config import settings
from app.coupon_service import refresh_expired_coupons
from app.database import SessionLocal, get_db
from app.models import Coupon, Listing, Order, Review, User
from app.pagination import paginate
from app.push_notifications import send_order_remind_push
from app.schemas import CreateOrderRequest, OrderDto, OrderReviewDto, Paginated, ReviewRequest, UpdateOrderRequest
from app.serializers import iso, order_to_dto

router = APIRouter(prefix="/orders", tags=["orders"])

OPEN_ORDER_STATUSES = ("pendingPay", "pendingShip", "pendingReceive", "pendingReview")


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
        .filter(Order.listing_id == listing_id, Order.status.in_(OPEN_ORDER_STATUSES))
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
    return paginate([order_to_dto(o, lang) for o in items], page, pageSize, total)


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
    return paginate([order_to_dto(o, lang, include_buyer=True) for o in items], page, pageSize, total)


@router.get("/{order_id}", response_model=OrderDto)
def get_order(order_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = (
        db.query(Order)
        .options(joinedload(Order.listing), joinedload(Order.seller))
        .filter(Order.id == order_id, Order.buyer_id == user.id)
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Order not found", "details": {}})
    return order_to_dto(order, get_accept_language(request))


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
        set_bundle_item_status(listing, bundle_item_id, "onHold")
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
    order = Order(
        buyer_id=user.id,
        listing_id=listing.id,
        seller_id=listing.seller_id,
        status="pendingPay",
        amount=payable,
        escrow_fee=settings.escrow_fee if listing.escrow_supported else 0.0,
        delivery_method=body.deliveryMethod,
        payment_method_id=body.paymentMethodId,
        bundle_item_id=bundle_item_id,
        coupon_id=coupon_id,
        discount_amount=discount,
    )
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
        order.escrow_fee = settings.escrow_fee if listing.escrow_supported else 0.0
        coupon_id = patch["couponId"] if "couponId" in patch else order.coupon_id
        payable, discount, resolved_coupon = _apply_coupon(
            db, user.id, coupon_id, base, exclude_order_id=order.id
        )
        order.amount = payable
        order.discount_amount = discount
        order.coupon_id = resolved_coupon
    order.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(order)
    return order_to_dto(order, get_accept_language(request))


@router.post("/{order_id}/pay", response_model=OrderDto)
def pay_order(order_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = _get_buyer_order(db, order_id, user.id)
    if order.status != "pendingPay":
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATE", "message": "Order is not pending payment", "details": {}})
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
        order.escrow_fee = settings.escrow_fee if listing.escrow_supported else 0.0
    _mark_coupon_used(db, order.coupon_id)
    order.status = "pendingShip"
    order.updated_at = datetime.now(timezone.utc)
    if listing and listing.status == "active":
        if order.bundle_item_id:
            apply_bundle_item_payment(listing, order.bundle_item_id)
        elif listing.type == "bundle":
            apply_bundle_payment(listing)
        else:
            listing.status = "sold"
    db.commit()
    db.refresh(order)
    return order_to_dto(order, get_accept_language(request))


@router.post("/{order_id}/ship", response_model=OrderDto)
def ship_order(order_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = _get_seller_order(db, order_id, user.id)
    if order.status != "pendingShip":
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATE", "message": "Order is not pending ship", "details": {}})
    order.status = "pendingReceive"
    order.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(order)
    return order_to_dto(order, get_accept_language(request), include_buyer=True)


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
    if order.status != "pendingReceive":
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATE", "message": "Order is not pending receive", "details": {}})
    order.status = "pendingReview"
    order.updated_at = datetime.now(timezone.utc)
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
    order = _get_buyer_order(db, order_id, user.id)
    review = db.query(Review).filter(Review.order_id == order.id).first()
    if not review:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "Review not found", "details": {}},
        )
    return OrderReviewDto(rating=review.rating, comment=review.comment, createdAt=iso(review.created_at))


@router.post("/{order_id}/review", status_code=204)
def submit_review(
    order_id: int,
    body: ReviewRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = _get_buyer_order(db, order_id, user.id)
    if order.status != "pendingReview":
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATE", "message": "Order is not pending review", "details": {}})
    if db.query(Review).filter(Review.order_id == order.id).first():
        raise HTTPException(status_code=409, detail={"code": "ALREADY_EXISTS", "message": "Review already submitted", "details": {}})
    db.add(Review(order_id=order.id, reviewer_id=user.id, rating=body.rating, comment=body.comment))
    order.status = "completed"
    order.updated_at = datetime.now(timezone.utc)
    listing = db.query(Listing).filter(Listing.id == order.listing_id).first()
    if listing:
        listing.status = "sold"
    db.commit()
    return Response(status_code=204)


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
