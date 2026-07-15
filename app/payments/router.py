"""Payment API routes (/v1/payments/*)."""



from __future__ import annotations



import json
from datetime import datetime, timezone



from fastapi import APIRouter, Depends, HTTPException, Request

from pydantic import BaseModel, Field

from sqlalchemy.orm import Session



from app.auth import get_current_user

from app.config import settings

from app.database import get_db

from app.models import Order, User
from app.payments.paypal_adapter import PayPalAdapter

from app.payments.service import apply_checkout_to_order, start_checkout

from app.payments.webhooks import handle_paypal_webhook, handle_stripe_webhook



router = APIRouter(prefix="/payments", tags=["payments"])





class CheckoutRequest(BaseModel):

    orderId: int

    paymentMethod: str = Field(default="card", pattern="^(card|apple|google|alipay|wechat|paypal)$")

    nativePaymentSheet: bool = False





class CheckoutResponse(BaseModel):

    psp: str

    paymentStatus: str

    clientSecret: str | None = None

    checkoutUrl: str | None = None

    simulated: bool = False

    publishableKey: str | None = None

    customerId: str | None = None

    ephemeralKey: str | None = None





@router.post("/checkout", response_model=CheckoutResponse)

def create_checkout(

    body: CheckoutRequest,

    user: User = Depends(get_current_user),

    db: Session = Depends(get_db),

):

    order = db.query(Order).filter(Order.id == body.orderId, Order.buyer_id == user.id).first()

    if not order:

        raise HTTPException(

            status_code=404,

            detail={"code": "NOT_FOUND", "message": "Order not found", "details": {}},

        )

    if order.status != "pendingPay":

        raise HTTPException(

            status_code=400,

            detail={"code": "INVALID_STATE", "message": "Order is not pending payment", "details": {}},

        )

    try:

        result = start_checkout(

            order,

            payment_method=body.paymentMethod,

            db=db,

            native_payment_sheet=body.nativePaymentSheet,

        )

    except RuntimeError as exc:

        raise HTTPException(

            status_code=502,

            detail={"code": "PAYMENT_PROVIDER_ERROR", "message": str(exc), "details": {}},

        ) from exc

    apply_checkout_to_order(order, result, body.paymentMethod)

    db.commit()

    return CheckoutResponse(

        psp=result.psp,

        paymentStatus=result.payment_status,

        clientSecret=result.client_secret,

        checkoutUrl=result.checkout_url,

        simulated=settings.payments_simulated,

        publishableKey=result.publishable_key,

        customerId=result.customer_id,

        ephemeralKey=result.ephemeral_key,

    )





class ConfirmRequest(BaseModel):
    orderId: int


@router.post("/checkout/confirm", response_model=CheckoutResponse)
def confirm_checkout(
    body: ConfirmRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Re-check a PaymentIntent after in-app 3-D Secure and sync payment_status. Lets the
    app finalise (call /orders/{id}/pay) without waiting on the webhook. No-op simulated."""
    order = db.query(Order).filter(Order.id == body.orderId, Order.buyer_id == user.id).first()
    if not order:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Order not found", "details": {}})
    if order.psp == "stripe" and order.psp_payment_id and settings.stripe_secret_key.strip():
        from app import stripe_service

        try:
            intent = stripe_service.retrieve_payment_intent(order.psp_payment_id)
        except Exception:
            intent = {}
        status = intent.get("status")
        if status == "succeeded" and order.status == "pendingPay":
            order.payment_status = "succeeded"
            order.psp_transaction_id = intent.get("id")
            db.commit()
    return CheckoutResponse(
        psp=order.psp or "stripe",
        paymentStatus=order.payment_status or "",
        clientSecret=None,
        checkoutUrl=None,
        simulated=settings.payments_simulated,
    )


@router.post("/webhooks/stripe")

async def stripe_webhook(request: Request, db: Session = Depends(get_db)):

    payload = await request.body()

    signature = request.headers.get("stripe-signature")

    if not handle_stripe_webhook(db, payload, signature):

        raise HTTPException(status_code=400, detail={"code": "WEBHOOK_REJECTED", "message": "Invalid webhook", "details": {}})

    return {"ok": True}





@router.post("/webhooks/paypal")

async def paypal_webhook(request: Request, db: Session = Depends(get_db)):

    payload = json.loads(await request.body())

    if not handle_paypal_webhook(db, payload):

        raise HTTPException(status_code=400, detail={"code": "WEBHOOK_REJECTED", "message": "Unhandled event", "details": {}})

    return {"ok": True}





@router.get("/paypal/return")

def paypal_return(orderId: int, db: Session = Depends(get_db)):

    order = db.query(Order).filter(Order.id == orderId).first()

    if order and order.status == "pendingPay" and settings.payments_simulated:

        order.payment_status = "succeeded"

        from app.payments.fulfillment import fulfill_paid_order



        fulfill_paid_order(db, order)

    elif order and order.status == "pendingPay" and order.psp == "paypal" and order.psp_payment_id:

        try:

            payload = PayPalAdapter().capture_order(order.psp_payment_id)

        except RuntimeError as exc:

            raise HTTPException(

                status_code=502,

                detail={"code": "PAYMENT_PROVIDER_ERROR", "message": str(exc), "details": {}},

            ) from exc

        capture = None

        for unit in payload.get("purchase_units", []):

            payments = unit.get("payments") or {}

            captures = payments.get("captures") or []

            if captures:

                capture = captures[0]

                break

        if payload.get("status") == "COMPLETED" or (capture and capture.get("status") == "COMPLETED"):

            order.payment_status = "succeeded"

            order.psp_transaction_id = capture.get("id") if capture else payload.get("id")

            from app.payments.fulfillment import fulfill_paid_order

            fulfill_paid_order(db, order)

    return {"ok": True, "orderId": orderId, "status": order.status if order else None}



@router.get("/paypal/cancel")

def paypal_cancel(orderId: int, db: Session = Depends(get_db)):

    order = db.query(Order).filter(Order.id == orderId).first()

    if order and order.status == "pendingPay" and order.psp == "paypal":

        order.payment_status = "cancelled"

        order.status = "cancelled"

        order.updated_at = datetime.now(timezone.utc)

        db.commit()

    return {"ok": True, "orderId": orderId, "status": order.status if order else None}





@router.get("/stripe/return")

def stripe_return(orderId: int, session_id: str | None = None, db: Session = Depends(get_db)):

    order = db.query(Order).filter(Order.id == orderId).first()

    if order and order.status == "pendingPay" and settings.payments_simulated:

        order.payment_status = "succeeded"

        from app.payments.fulfillment import fulfill_paid_order



        fulfill_paid_order(db, order)

    elif order and order.status == "pendingPay" and order.psp == "stripe" and settings.stripe_secret_key.strip():

        from app import stripe_service
        from app.payments.fulfillment import fulfill_paid_order

        try:
            session = stripe_service.retrieve_checkout_session(session_id or order.psp_payment_id)
        except Exception:
            session = {}

        if session.get("payment_status") == "paid" or session.get("status") == "complete":
            order.payment_status = "succeeded"
            order.psp_transaction_id = session.get("payment_intent") or session.get("id")
            fulfill_paid_order(db, order)

    return {"ok": True, "orderId": orderId, "sessionId": session_id, "status": order.status if order else None}



@router.get("/stripe/cancel")

def stripe_cancel(orderId: int, db: Session = Depends(get_db)):

    order = db.query(Order).filter(Order.id == orderId).first()

    if order and order.status == "pendingPay" and order.psp == "stripe":

        order.payment_status = "cancelled"

        order.status = "cancelled"

        order.updated_at = datetime.now(timezone.utc)

        db.commit()

    return {"ok": True, "orderId": orderId, "status": order.status if order else None}
