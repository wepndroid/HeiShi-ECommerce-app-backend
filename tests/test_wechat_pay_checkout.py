from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.config import settings
from app.payments.stripe_adapter import StripeAdapter


class WeChatPayCheckoutTests(unittest.TestCase):
    def test_hosted_checkout_declares_wechat_web_client(self) -> None:
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "id": "cs_test_wechat",
            "status": "open",
            "url": "https://checkout.stripe.com/c/pay/cs_test_wechat",
        }
        client = Mock()
        client.post.return_value = response
        client_context = Mock()
        client_context.__enter__ = Mock(return_value=client)
        client_context.__exit__ = Mock(return_value=False)

        with (
            patch.object(settings, "stripe_secret_key", "sk_test_wechat"),
            patch.object(settings, "base_url", "http://127.0.0.1:8000"),
            patch("app.payments.stripe_adapter.httpx.Client", return_value=client_context),
        ):
            result = StripeAdapter().create_checkout(
                order_id=82,
                amount_minor=55500,
                currency="aud",
                buyer_id="buyer-wechat",
                payment_method="wechat",
            )

        request_data = client.post.call_args.kwargs["data"]
        self.assertEqual(request_data["payment_method_types[0]"], "wechat_pay")
        self.assertEqual(
            request_data["payment_method_options[wechat_pay][client]"],
            "web",
        )
        self.assertEqual(result.psp_payment_id, "cs_test_wechat")
        self.assertEqual(result.checkout_url, "https://checkout.stripe.com/c/pay/cs_test_wechat")


if __name__ == "__main__":
    unittest.main()
