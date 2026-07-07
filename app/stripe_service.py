"""Thin Stripe wrapper for the HeyMarket escrow marketplace.

Every function raises :class:`StripeNotConfigured` when Stripe is not available
(package missing or no secret key), so callers branch on ``settings.stripe_enabled``
and fall back to the simulated path — keeping local/offline dev fully working while
the real flow goes live the moment the client provides Stripe keys.

Flows (per Stripe's official React Native guidance):
- Buyer saved card/wallet: Customer + EphemeralKey + SetupIntent -> mobile PaymentSheet.
- Seller payout: Connect Express account + AccountLink onboarding -> payouts_enabled.
- Checkout: PaymentIntent charged against the buyer's saved PaymentMethod.
"""
from __future__ import annotations

from app.config import settings

try:  # stripe is optional until the client provides keys
    import stripe  # type: ignore
except Exception:  # pragma: no cover
    stripe = None  # type: ignore

# API version the ephemeral key is minted with; must be one the mobile SDK accepts.
STRIPE_API_VERSION = "2024-06-20"


class StripeNotConfigured(RuntimeError):
    """Raised when a real Stripe call is attempted without configuration."""


def _client():
    if stripe is None:
        raise StripeNotConfigured("stripe package is not installed")
    key = settings.stripe_secret_key.strip()
    if not key:
        raise StripeNotConfigured("STRIPE_SECRET_KEY is not set")
    stripe.api_key = key
    return stripe


# --- Customer + saved cards (buyer) --------------------------------------------------

def ensure_customer(user) -> str:
    """Return the user's Stripe Customer id, creating one on first use."""
    existing = getattr(user, "stripe_customer_id", None)
    if existing:
        return existing
    s = _client()
    customer = s.Customer.create(
        name=user.nickname or None,
        email=getattr(user, "email", None) or None,
        phone=getattr(user, "phone", None) or None,
        metadata={"app_user_id": user.id},
    )
    return customer["id"]


def create_setup_intent(customer_id: str) -> dict:
    """Params the mobile PaymentSheet needs to save a card for reuse."""
    s = _client()
    ephemeral_key = s.EphemeralKey.create(
        customer=customer_id,
        stripe_version=STRIPE_API_VERSION,
    )
    setup_intent = s.SetupIntent.create(
        customer=customer_id,
        automatic_payment_methods={"enabled": True},
    )
    return {
        "setupIntentClientSecret": setup_intent["client_secret"],
        "ephemeralKey": ephemeral_key["secret"],
        "customerId": customer_id,
        "publishableKey": settings.stripe_publishable_key,
    }


def retrieve_payment_method(payment_method_id: str) -> dict:
    return _client().PaymentMethod.retrieve(payment_method_id)


def list_card_payment_methods(customer_id: str) -> list:
    s = _client()
    return s.PaymentMethod.list(customer=customer_id, type="card").get("data", [])


def detach_payment_method(payment_method_id: str) -> None:
    try:
        _client().PaymentMethod.detach(payment_method_id)
    except Exception:  # already detached / gone — safe to ignore
        pass


# --- Connect Express (seller payouts) ------------------------------------------------

def ensure_connect_account(user) -> str:
    existing = getattr(user, "stripe_connect_id", None)
    if existing:
        return existing
    s = _client()
    account = s.Account.create(
        type="express",
        email=getattr(user, "email", None) or None,
        capabilities={"transfers": {"requested": True}},
        business_type="individual",
        metadata={"app_user_id": user.id},
    )
    return account["id"]


def create_account_onboarding_link(account_id: str) -> str:
    link = _client().AccountLink.create(
        account=account_id,
        type="account_onboarding",
        refresh_url=settings.connect_refresh_url,
        return_url=settings.connect_return_url,
    )
    return link["url"]


def retrieve_account(account_id: str) -> dict:
    return _client().Account.retrieve(account_id)


# --- PaymentIntent (checkout) --------------------------------------------------------

def create_and_confirm_payment_intent(
    *,
    amount_minor: int,
    currency: str,
    customer_id: str,
    payment_method_id: str,
    description: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Charge the buyer's saved payment method. Returns the PaymentIntent; the caller
    inspects ``status`` (``succeeded`` / ``requires_action`` / ``requires_payment_method``)."""
    s = _client()
    return s.PaymentIntent.create(
        amount=amount_minor,
        currency=currency,
        customer=customer_id,
        payment_method=payment_method_id,
        confirm=True,
        off_session=False,
        description=description,
        metadata=metadata or {},
        automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
    )


def retrieve_payment_intent(payment_intent_id: str) -> dict:
    return _client().PaymentIntent.retrieve(payment_intent_id)


def construct_webhook_event(payload: bytes, sig_header: str):
    return _client().Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret.strip())
