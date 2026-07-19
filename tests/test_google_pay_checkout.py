from __future__ import annotations

import unittest
from unittest.mock import patch

from app.config import settings
from app.payments.stripe_adapter import StripeAdapter


class GooglePayCheckoutTests(unittest.TestCase):
    def test_native_google_pay_uses_payment_intent_not_hosted_checkout(self) -> None:
        intent = {
            "id": "pi_google_test",
            "status": "requires_payment_method",
            "client_secret": "pi_google_test_secret_123",
        }

        with (
            patch.object(settings, "stripe_secret_key", "sk_test_google"),
            patch.object(settings, "stripe_publishable_key", "pk_test_google"),
            patch(
                "app.stripe_service.create_payment_sheet_intent",
                return_value=intent,
            ) as create_intent,
            patch(
                "app.stripe_service.create_customer_ephemeral_key",
                return_value="ephkey_google",
            ),
        ):
            result = StripeAdapter().create_checkout(
                order_id=81,
                amount_minor=1100,
                currency="aud",
                buyer_id="buyer-google",
                payment_method="google",
                customer_id="cus_google",
                native_payment_sheet=True,
            )

        self.assertEqual(result.psp, "stripe")
        self.assertEqual(result.psp_payment_id, "pi_google_test")
        self.assertEqual(result.client_secret, "pi_google_test_secret_123")
        self.assertEqual(result.publishable_key, "pk_test_google")
        self.assertEqual(result.customer_id, "cus_google")
        self.assertEqual(result.ephemeral_key, "ephkey_google")
        self.assertIsNone(result.checkout_url)
        create_intent.assert_called_once_with(
            amount_minor=1100,
            currency="aud",
            customer_id="cus_google",
            description="HeyMarket order #81",
            metadata={"order_id": "81", "buyer_id": "buyer-google"},
            transfer_group="order_81",
        )


if __name__ == "__main__":
    unittest.main()
