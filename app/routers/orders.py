from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session, joinedload

from app.auth import get_accept_language, get_current_user
from app.config import settings
from app.database import get_db
from app.models import Listing, Order, Review, User
from app.pagination import paginate
from app.schemas import CreateOrderRequest, OrderDto, Paginated, ReviewRequest
from app.serializers import order_to_dto

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("", response_model=Paginated[OrderDto])
def list_orders(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lang = get_accept_language(request)
    q = (
        db.query(Order)
        .options(joinedload(Order.listing), joinedload(Order.seller))
        .filter(Order.buyer_id == user.id)
    )
    if status and status != "all":
        q = q.filter(Order.status == status)
    q = q.order_by(Order.created_at.desc())
    total = q.count()
    items = q.offset((page - 1) * pageSize).limit(pageSize).all()
    return paginate([order_to_dto(o, lang) for o in items], page, pageSize, total)


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
    listing = db.query(Listing).options(joinedload(Listing.seller)).filter(Listing.id == body.listingId).first()
    if not listing or listing.status != "active":
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Listing not available", "details": {}})
    if listing.seller_id == user.id:
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATE", "message": "Cannot buy own listing", "details": {}})
    order = Order(
        buyer_id=user.id,
        listing_id=listing.id,
        seller_id=listing.seller_id,
        status="pendingPay",
        amount=listing.price,
        escrow_fee=settings.escrow_fee,
        delivery_method=body.deliveryMethod,
        payment_method_id=body.paymentMethodId,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    order.listing = listing
    order.seller = listing.seller
    return order_to_dto(order, get_accept_language(request))


@router.post("/{order_id}/pay", response_model=OrderDto)
def pay_order(order_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = _get_buyer_order(db, order_id, user.id)
    if order.status != "pendingPay":
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATE", "message": "Order is not pending payment", "details": {}})
    order.status = "pendingShip"
    order.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(order)
    return order_to_dto(order, get_accept_language(request))


@router.post("/{order_id}/remind-ship", status_code=204)
def remind_ship(order_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = _get_buyer_order(db, order_id, user.id)
    if order.status != "pendingShip":
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATE", "message": "Cannot remind ship for this order", "details": {}})
    return Response(status_code=204)


@router.post("/{order_id}/confirm-receive", response_model=OrderDto)
def confirm_receive(order_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = _get_buyer_order(db, order_id, user.id)
    if order.status not in ("pendingShip", "pendingReceive"):
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATE", "message": "Order is not pending receive", "details": {}})
    order.status = "pendingReview"
    order.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(order)
    return order_to_dto(order, get_accept_language(request))


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
