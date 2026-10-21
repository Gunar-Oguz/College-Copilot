"""
Stripe billing for CollegeCopilot Pro ($9/month).

- Checkout Sessions (mode=subscription) for signing up, the Customer Portal for managing billing.
- Each user gets a Stripe Customer before checkout; its id is stored in user_plans, so every
  subscription event maps back to our user through the customer (metadata is only a fallback).
- Webhooks are verified with STRIPE_WEBHOOK_SECRET and processed once per event id.
- sync_from_stripe() refreshes a user's plan straight from Stripe (used after checkout, so the
  success page is right even before the webhook arrives).

Environment: STRIPE_SECRET_KEY (a restricted rk_ key is recommended), STRIPE_WEBHOOK_SECRET,
optional STRIPE_PRICE_ID (otherwise the price is found or created by lookup key).
"""

import logging
import os
from datetime import datetime, timezone

import stripe

import plans

logger = logging.getLogger(__name__)

PRICE_LOOKUP_KEY = "collegecopilot_pro_monthly"
PRICE_CENTS = 900
INTEGRATION_ID = "collegecopilot_pro_checkout_qmxvtrkd"

_client = None
_price_id = None


class BillingError(Exception):
    pass


def _plain(obj) -> dict:
    """Stripe objects aren't dicts in stripe-python 16; read fields from a plain copy."""
    return obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)


def configured() -> bool:
    return bool(os.getenv("STRIPE_SECRET_KEY"))


def client() -> stripe.StripeClient:
    global _client
    if _client is None:
        if not configured():
            raise BillingError("Payments aren't set up on this server yet.")
        _client = stripe.StripeClient(os.environ["STRIPE_SECRET_KEY"])
    return _client


def pro_price_id() -> str:
    """The $9/month Pro price: STRIPE_PRICE_ID, or found / created by lookup key."""
    global _price_id
    if _price_id:
        return _price_id
    if os.getenv("STRIPE_PRICE_ID"):
        _price_id = os.environ["STRIPE_PRICE_ID"]
        return _price_id
    found = client().v1.prices.list(params={"lookup_keys": [PRICE_LOOKUP_KEY], "active": True, "limit": 1})
    if found.data:
        _price_id = found.data[0].id
        return _price_id
    product = client().v1.products.create(params={
        "name": "CollegeCopilot Pro",
        "description": "Unlimited AI questions, unlimited saved schools, 10-school compare, deadline "
                       "calendar, CSV export, all reviews, fit analysis and application checklists.",
    })
    price = client().v1.prices.create(params={
        "product": product.id,
        "currency": "usd",
        "unit_amount": PRICE_CENTS,
        "recurring": {"interval": "month"},
        "lookup_key": PRICE_LOOKUP_KEY,
        "nickname": "Pro monthly",
    })
    _price_id = price.id
    return _price_id


def _customer_for(user_id: str, email: str | None) -> str:
    customer_id = plans.customer_id_for(user_id)
    if customer_id:
        return customer_id
    customer = client().v1.customers.create(params={
        "email": email or None,
        "metadata": {"user_id": user_id},
    }, options={"idempotency_key": f"customer-{user_id}"})
    plans.save_customer(user_id, customer.id)
    return customer.id


def create_checkout_session(user_id: str, email: str | None, origin: str) -> str:
    """URL of a Stripe Checkout page for the Pro subscription."""
    if plans.is_pro(user_id):
        raise BillingError("You're already on Pro.")
    session = client().v1.checkout.sessions.create(params={
        "mode": "subscription",
        "customer": _customer_for(user_id, email),
        "client_reference_id": user_id,
        "line_items": [{"price": pro_price_id(), "quantity": 1}],
        "subscription_data": {"metadata": {"user_id": user_id}},
        "allow_promotion_codes": True,
        "success_url": f"{origin}/pricing?success=true&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{origin}/pricing?cancelled=true",
        "integration_identifier": INTEGRATION_ID,
    })
    return session.url


def create_portal_session(user_id: str, origin: str) -> str:
    customer_id = plans.customer_id_for(user_id)
    if not customer_id:
        raise BillingError("No billing account yet. Upgrade to Pro first.")
    portal = client().v1.billing_portal.sessions.create(params={
        "customer": customer_id, "return_url": f"{origin}/pricing",
    })
    return portal.url


def cancel_subscription(user_id: str) -> dict:
    """Cancel at the end of the current billing period (Pro stays on until then)."""
    rows = plans.execute_write("SELECT stripe_subscription_id FROM user_plans WHERE user_id = %s", (user_id,))
    subscription_id = rows[0]["stripe_subscription_id"] if rows else None
    if not subscription_id or not plans.is_pro(user_id):
        raise BillingError("You don't have an active Pro subscription.")
    subscription = client().v1.subscriptions.update(subscription_id, params={"cancel_at_period_end": True})
    _store_subscription(subscription, user_id)
    return plans.get_plan(user_id)


def _period_end(subscription) -> datetime | None:
    """current_period_end lives on subscription items in recent API versions."""
    value = subscription.get("current_period_end")
    if not value:
        items = (subscription.get("items") or {}).get("data") or []
        value = items[0].get("current_period_end") if items else None
    return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None) if value else None


def _store_subscription(subscription, user_id: str | None = None) -> None:
    subscription = _plain(subscription)
    customer_id = subscription.get("customer")
    user_id = user_id or plans.user_for_customer(customer_id) \
        or (subscription.get("metadata") or {}).get("user_id")
    if not user_id:
        logger.warning("Stripe subscription %s has no matching user", subscription.get("id"))
        return
    plans.save_subscription(
        user_id, customer_id, subscription["id"], subscription["status"],
        _period_end(subscription), bool(subscription.get("cancel_at_period_end")),
    )


def sync_from_stripe(user_id: str) -> dict:
    """Refresh a user's plan from their most recent Stripe subscription."""
    customer_id = plans.customer_id_for(user_id)
    if customer_id and configured():
        subscriptions = client().v1.subscriptions.list(params={"customer": customer_id, "status": "all", "limit": 5})
        if subscriptions.data:
            latest = max((_plain(s) for s in subscriptions.data), key=lambda s: s.get("created") or 0)
            _store_subscription(latest, user_id)
    return plans.get_plan(user_id)


SUBSCRIPTION_EVENTS = {"customer.subscription.created", "customer.subscription.updated",
                       "customer.subscription.deleted", "customer.subscription.paused",
                       "customer.subscription.resumed"}
RISK_EVENTS = {"charge.dispute.created", "charge.refunded", "radar.early_fraud_warning.created"}


def handle_webhook(payload: bytes, signature: str | None) -> str:
    """Verify and apply a Stripe webhook. Returns a short status for logs."""
    secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise BillingError("Webhook secret not configured")
    event = client().construct_event(payload, signature, secret)  # raises on a bad signature
    if not plans.first_time_event(event.id, event.type):
        return "duplicate"
    obj = _plain(event.data.object)
    if event.type in SUBSCRIPTION_EVENTS:
        _store_subscription(obj)
    elif event.type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if obj.get("mode") == "subscription" and obj.get("subscription"):
            user_id = plans.user_for_customer(obj.get("customer")) or obj.get("client_reference_id")
            subscription = client().v1.subscriptions.retrieve(obj["subscription"])
            _store_subscription(subscription, user_id)
    elif event.type in ("invoice.paid", "invoice.payment_failed"):
        subscription_id = _invoice_subscription(obj)
        if subscription_id:
            _store_subscription(client().v1.subscriptions.retrieve(subscription_id))
        if event.type == "invoice.payment_failed":
            logger.warning("Payment failed for customer %s (invoice %s)", obj.get("customer"), obj.get("id"))
    elif event.type in RISK_EVENTS:
        logger.warning("Stripe %s: %s (customer %s). Review in the Stripe Dashboard.",
                       event.type, obj.get("id"), obj.get("customer"))
    return event.type


def _invoice_subscription(invoice) -> str | None:
    """The subscription an invoice belongs to (its location differs across API versions)."""
    if invoice.get("subscription"):
        return invoice["subscription"]
    details = ((invoice.get("parent") or {}).get("subscription_details") or {})
    return details.get("subscription")
