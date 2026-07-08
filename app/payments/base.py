"""Payment adapter base types (PROG-408)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class CheckoutResult:
    psp: str
    payment_status: str
    client_secret: str | None = None
    checkout_url: str | None = None
    psp_payment_id: str | None = None


class PaymentAdapter(Protocol):
    psp: str

    def create_checkout(
        self,
        *,
        order_id: int,
        amount_minor: int,
        currency: str,
        buyer_id: str,
        payment_method: str,
        customer_id: str | None = None,
        payment_method_id: str | None = None,
    ) -> CheckoutResult: ...
