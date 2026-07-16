from unittest import TestCase
from unittest.mock import Mock, patch

from fastapi import HTTPException

from app.config import settings
from app.models import Order
from app.payments.refunds import refund_order_payment
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
