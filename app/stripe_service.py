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

from urllib.parse import urlparse

from app.config import settings

try:  # Dependency is pinned in requirements; keep startup tolerant for simulated mode.
    import stripe  # type: ignore
except Exception:  # pragma: no cover
    stripe = None  # type: ignore

# API version the ephemeral key is minted with; must be one the mobile SDK accepts.
STRIPE_API_VERSION = "2024-06-20"


class StripeNotConfigured(RuntimeError):
    """Raised when a real Stripe call is attempted without configuration."""


class StripeConnectCountryMismatch(RuntimeError):
    """A completed connected account has a different immutable legal country."""


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
    """Return a correctly configured seller account.

    Stripe Connect account country is immutable after onboarding. Replace an existing
    wrong-country account only while it is still incomplete; never silently replace a
    completed payout identity.
    """
    existing = getattr(user, "stripe_connect_id", None)
    target_country = settings.stripe_connect_country.strip().upper()
    s = _client()
    if existing:
        account = s.Account.retrieve(existing)
        existing_country = str(account.get("country") or "").upper()
        if existing_country == target_country:
            return existing
        if account.get("details_submitted") or account.get("payouts_enabled"):
            raise StripeConnectCountryMismatch(
                f"Connected account country {existing_country or 'unknown'} does not match {target_country}"
            )
    account = s.Account.create(
        type="express",
        country=target_country,
        email=getattr(user, "email", None) or None,
        # Stripe requires AU Express accounts that request `transfers` to request
        # `card_payments` as well, even though this marketplace charges buyers on
        # the platform and uses separate transfers for seller release.
        capabilities={
            "card_payments": {"requested": True},
            "transfers": {"requested": True},
        },
        business_type="individual",
        metadata={"app_user_id": user.id},
    )
    return account["id"]


def _connect_web_callback(configured_url: str, action: str) -> str:
    """Return an Account Link callback Stripe accepts.

    Stripe requires an HTTP(S) return/refresh URL and rejects app schemes such as
    ``heishi://``. For those configured app links, route through the backend and let
    that web endpoint hand control back to the mobile app.
    """
    parsed = urlparse(configured_url.strip())
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return configured_url.strip()
    return f"{settings.base_url.rstrip('/')}/v1/payouts/connect/{action}"


def create_account_onboarding_link(account_id: str) -> str:
    link = _client().AccountLink.create(
        account=account_id,
        type="account_onboarding",
        refresh_url=_connect_web_callback(settings.connect_refresh_url, "refresh"),
        return_url=_connect_web_callback(settings.connect_return_url, "return"),
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
    transfer_group: str | None = None,
) -> dict:
    """Charge the buyer's saved payment method. Returns the PaymentIntent; the caller
    inspects ``status`` (``succeeded`` / ``requires_action`` / ``requires_payment_method``)."""
    s = _client()
    params: dict = {
        "amount": amount_minor,
        "currency": currency,
        "customer": customer_id,
        "payment_method": payment_method_id,
        "confirm": True,
        "off_session": False,
        "description": description,
        "metadata": metadata or {},
        "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
    }
    if transfer_group:
        params["transfer_group"] = transfer_group
    return s.PaymentIntent.create(**params)


def create_payment_sheet_intent(
    *,
    amount_minor: int,
    currency: str,
    customer_id: str,
    description: str | None = None,
    metadata: dict | None = None,
    transfer_group: str | None = None,
) -> dict:
    """Create an unconfirmed card PaymentIntent for native mobile PaymentSheet."""
    params: dict = {
        "amount": amount_minor,
        "currency": currency,
        "customer": customer_id,
        "description": description,
        "metadata": metadata or {},
        "payment_method_types": ["card"],
        "setup_future_usage": "off_session",
    }
    if transfer_group:
        params["transfer_group"] = transfer_group
    return _client().PaymentIntent.create(**params)


def create_customer_ephemeral_key(customer_id: str) -> str:
    key = _client().EphemeralKey.create(
        customer=customer_id,
        stripe_version=STRIPE_API_VERSION,
    )
    return key["secret"]


def retrieve_payment_intent(payment_intent_id: str) -> dict:
    return _client().PaymentIntent.retrieve(payment_intent_id)


def construct_webhook_event(payload: bytes, sig_header: str):
    return _client().Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret.strip())


def retrieve_checkout_session(session_id: str) -> dict:
    return _client().checkout.Session.retrieve(session_id)


def resolve_transfer_source_transaction(reference_id: str) -> str | None:
    """Resolve a PaymentIntent / Checkout Session to the underlying charge id."""
    if not reference_id:
        return None
    if reference_id.startswith("cs_"):
        session = retrieve_checkout_session(reference_id)
        payment_intent_id = session.get("payment_intent")
        if not payment_intent_id:
            return None
        intent = retrieve_payment_intent(payment_intent_id)
    else:
        intent = retrieve_payment_intent(reference_id)
    latest_charge = intent.get("latest_charge")
    if isinstance(latest_charge, str):
        return latest_charge
    if isinstance(latest_charge, dict):
        return latest_charge.get("id")
    return None


def create_transfer(
    *,
    amount_minor: int,
    currency: str,
    destination_account_id: str,
    source_transaction: str | None = None,
    transfer_group: str | None = None,
    metadata: dict | None = None,
) -> dict:
    params: dict = {
        "amount": amount_minor,
        "currency": currency,
        "destination": destination_account_id,
        "metadata": metadata or {},
    }
    if source_transaction:
        params["source_transaction"] = source_transaction
    if transfer_group:
        params["transfer_group"] = transfer_group
    return _client().Transfer.create(**params)


def create_transfer_reversal(
    *,
    transfer_id: str,
    amount_minor: int | None = None,
    metadata: dict | None = None,
) -> dict:
    params: dict = {"metadata": metadata or {}}
    if amount_minor is not None:
        params["amount"] = amount_minor
    return _client().Transfer.create_reversal(transfer_id, **params)


def create_refund(
    *,
    payment_intent_id: str,
    amount_minor: int | None = None,
    metadata: dict | None = None,
) -> dict:
    params: dict = {
        "payment_intent": payment_intent_id,
        "metadata": metadata or {},
    }
    if amount_minor is not None:
        params["amount"] = amount_minor
    return _client().Refund.create(**params)
