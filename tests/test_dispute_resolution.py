import json
from unittest import TestCase
from unittest.mock import Mock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models import Order
from app.payments.refunds import refund_order_payment
from app.payments.webhooks import handle_stripe_webhook
from app.payout_release import PayoutTransition
from app.routers import admin_routes


class DisputeResolutionTests(TestCase):
    def test_successful_refund_closes_pending_seller_payout(self):
        order = Order(
            id=101,
            buyer_id="buyer",
            seller_id="seller",
            listing_id=1,
            amount=25.0,
            escrow_fee=0.0,
            payment_status="succeeded",
            payout_status="pending",
            psp="paypal",
            psp_transaction_id="capture-test",
        )

        with patch.object(settings, "payments_simulated", True):
            result = refund_order_payment(order)

        self.assertEqual(result.status, "refunded")
        self.assertEqual(order.payment_status, "refunded")
        self.assertEqual(order.payout_status, "reversed")
        self.assertIsNotNone(order.payout_reversed_at)

    def test_admin_complete_does_not_report_success_when_release_fails(self):
        order = Order(
            id=102,
            buyer_id="buyer",
            seller_id="seller",
            listing_id=1,
            amount=25.0,
            status="inDispute",
            payment_status="succeeded",
            payout_status="blocked",
            payout_paused=True,
        )
        db = Mock()
        admin = Mock(id="admin", nickname="Admin")
        body = admin_routes.DisputeResolveRequest(resolution="complete", note="Award seller")

        failed = PayoutTransition(
            status="failed",
            code="PAYPAL_REFERENCED_PAYOUT_FAILED",
            reason="provider timeout",
        )
        with (
            patch.object(admin_routes, "_get_order_or_404", return_value=order),
            patch.object(admin_routes, "release_payout_for_order", return_value=failed),
        ):
            with self.assertRaises(HTTPException) as raised:
                admin_routes.resolve_dispute(102, body, db, admin)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "PAYPAL_REFERENCED_PAYOUT_FAILED")
        db.commit.assert_not_called()

    def test_pending_stripe_refund_is_not_reported_as_completed(self):
        order = Order(
            id=103,
            buyer_id="buyer",
            seller_id="seller",
            listing_id=1,
            amount=25.0,
            escrow_fee=0.0,
            payment_status="succeeded",
            payout_status="blocked",
            psp="stripe",
            psp_transaction_id="pi_test",
        )

        with (
            patch.object(settings, "payments_simulated", False),
            patch(
                "app.payments.refunds.stripe_service.create_refund",
                return_value={"id": "re_pending", "status": "pending"},
            ) as create_refund,
        ):
            result = refund_order_payment(order)
            repeated = refund_order_payment(order)

        self.assertEqual(result.status, "pending")
        self.assertEqual(repeated.status, "pending")
        self.assertEqual(order.payment_status, "succeeded")
        self.assertEqual(order.refund_status, "pending")
        self.assertEqual(order.refund_reference, "re_pending")
        self.assertTrue(order.payout_paused)
        create_refund.assert_called_once()

    def test_refund_updated_webhook_completes_pending_refund(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        order = Order(
            id=104,
            buyer_id="buyer",
            seller_id="seller",
            listing_id=1,
            amount=25.0,
            status="refundInProgress",
            payment_status="succeeded",
            refund_status="pending",
            refund_reference="re_async",
            payout_status="blocked",
            payout_paused=True,
            psp="stripe",
            psp_transaction_id="pi_async",
        )
        session.add(order)
        session.commit()
        event = {
            "type": "refund.updated",
            "data": {
                "object": {
                    "id": "re_async",
                    "status": "succeeded",
                    "payment_intent": "pi_async",
                    "metadata": {"order_id": "104"},
                }
            },
        }

        with patch.object(settings, "stripe_webhook_secret", ""):
            handled = handle_stripe_webhook(session, json.dumps(event).encode(), None)

        session.refresh(order)
        self.assertTrue(handled)
        self.assertEqual(order.refund_status, "succeeded")
        self.assertEqual(order.payment_status, "refunded")
        self.assertEqual(order.status, "refunded")
        self.assertEqual(order.payout_status, "reversed")
        self.assertFalse(order.payout_paused)
        self.assertIsNotNone(order.refunded_at)
        session.close()

    def test_refund_failed_webhook_keeps_seller_payout_blocked(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        order = Order(
            id=105,
            buyer_id="buyer",
            seller_id="seller",
            listing_id=1,
            amount=25.0,
            status="refundInProgress",
            payment_status="succeeded",
            refund_status="pending",
            refund_reference="re_failed",
            payout_status="blocked",
            payout_paused=True,
            psp="stripe",
            psp_transaction_id="pi_failed",
        )
        session.add(order)
        session.commit()
        event = {
            "type": "refund.failed",
            "data": {
                "object": {
                    "id": "re_failed",
                    "status": "failed",
                    "failure_reason": "declined",
                    "payment_intent": "pi_failed",
                    "metadata": {"order_id": "105"},
                }
            },
        }

        with patch.object(settings, "stripe_webhook_secret", ""):
            handled = handle_stripe_webhook(session, json.dumps(event).encode(), None)

        session.refresh(order)
        self.assertTrue(handled)
        self.assertEqual(order.refund_status, "failed")
        self.assertEqual(order.payment_status, "succeeded")
        self.assertEqual(order.status, "inDispute")
        self.assertEqual(order.payout_status, "blocked")
        self.assertTrue(order.payout_paused)
        self.assertEqual(order.refund_failure_reason, "declined")
        session.close()
