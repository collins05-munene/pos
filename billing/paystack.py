"""
Minimal Paystack client for the PLATFORM's own collections (shop subscription payments).

Flow: initialize_transaction() -> send the shop owner to the returned authorization_url ->
Paystack redirects back to our callback URL and also POSTs a signed `charge.success` webhook.
Both paths end in billing.services.settle_paystack_transaction(), which only ever acts on data
that came from verify_transaction() (our own authenticated call) or a correctly signed webhook.

Amounts: Paystack works in the currency's smallest unit (KES 1,499.00 -> 149900).
"""
import hashlib
import hmac
import logging
from decimal import Decimal, ROUND_HALF_UP

import requests
from django.conf import settings

BASE_URL = "https://api.paystack.co"
TIMEOUT = 20

logger = logging.getLogger(__name__)


class PaystackError(Exception):
    """Message is safe to show to the shop owner; technical detail goes to the log."""


def secret_key():
    key = (getattr(settings, "PAYSTACK_SECRET_KEY", "") or "").strip()
    if not key:
        raise PaystackError("Online payment isn't set up yet. Please contact support.")
    return key


def is_configured():
    return bool((getattr(settings, "PAYSTACK_SECRET_KEY", "") or "").strip())


def to_subunit(amount):
    """Decimal KES -> integer cents."""
    return int((Decimal(amount) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_subunit(value):
    """Integer cents -> Decimal KES."""
    return (Decimal(int(value)) / 100).quantize(Decimal("0.01"))


def _call(method, path, **kwargs):
    headers = {"Authorization": f"Bearer {secret_key()}", "Content-Type": "application/json"}
    try:
        resp = requests.request(method, f"{BASE_URL}{path}", headers=headers, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        logger.warning("Paystack request failed: %s", exc)
        raise PaystackError("We couldn't reach the payment provider. Please try again in a moment.") from exc
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400 or not body.get("status"):
        logger.warning("Paystack %s %s -> %s %s", method, path, resp.status_code, body.get("message"))
        if resp.status_code in (401, 403):
            raise PaystackError("Online payment isn't set up correctly yet. Please contact support.")
        raise PaystackError(body.get("message") or "The payment provider rejected the request.")
    return body.get("data") or {}


def initialize_transaction(*, email, amount, reference, callback_url, currency="KES", metadata=None):
    """-> {"authorization_url", "access_code", "reference"}"""
    payload = {
        "email": email,
        "amount": to_subunit(amount),
        "currency": currency,
        "reference": reference,
        "callback_url": callback_url,
        "metadata": metadata or {},
    }
    return _call("POST", "/transaction/initialize", json=payload)


def verify_transaction(reference):
    """-> Paystack's transaction object (status, amount, currency, channel, id, ...)."""
    return _call("GET", f"/transaction/verify/{reference}")


def valid_signature(raw_body, signature):
    """Webhook authenticity: HMAC-SHA512 of the raw request body, keyed with the secret key."""
    if not signature or not is_configured():
        return False
    expected = hmac.new(secret_key().encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())
