"""
Razorpay integration for the ₹199/mo Pro upgrade.

Flow: frontend calls create_order() to get a Razorpay order, opens
Razorpay's Checkout modal with it, and on success calls verify_payment()
with the payment id + signature Checkout hands back. We verify that
signature server-side (HMAC-SHA256 over "order_id|payment_id" using the
key secret) before trusting it and flipping the user to paid -- the
frontend result alone is never trusted, since it's attacker-controlled.

Talks to Razorpay's REST API directly via `requests` (already a
dependency) rather than pulling in their SDK for two endpoints.
"""

import hashlib
import hmac as hmac_lib
import os

import requests

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")

# Amounts in paise (Razorpay's smallest-currency-unit convention). Yearly
# is priced below 12x monthly as the annual-plan discount.
PLAN_PRICES_PAISE = {
    "monthly": 19900,   # ₹199/mo
    "yearly": 199000,   # ₹1,990/yr
}

_API_BASE = "https://api.razorpay.com/v1"


class PaymentsNotConfigured(Exception):
    pass


def _require_configured():
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise PaymentsNotConfigured(
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set -- payments aren't configured yet."
        )


def create_order(user_id: str, plan: str) -> dict:
    _require_configured()
    if plan not in PLAN_PRICES_PAISE:
        raise ValueError(f"Unknown plan '{plan}'")
    resp = requests.post(
        f"{_API_BASE}/orders",
        auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
        json={
            "amount": PLAN_PRICES_PAISE[plan],
            "currency": "INR",
            # `plan` here is the authoritative record of what was actually
            # paid for -- verify_payment reads it back from Razorpay's own
            # order record (see get_order()) rather than trusting whatever
            # plan the frontend claims when it calls /verify, since the
            # frontend's own account of the transaction is attacker-
            # controlled the same way the raw success callback is.
            "notes": {"user_id": user_id, "plan": plan},
        },
        timeout=15,
    )
    resp.raise_for_status()
    order = resp.json()
    return {
        "order_id": order["id"],
        "amount": order["amount"],
        "currency": order["currency"],
        "key_id": RAZORPAY_KEY_ID,
        "plan": plan,
    }


def get_order(order_id: str) -> dict:
    """Fetches the order back from Razorpay so callers can read its
    authoritative `notes` (user_id, plan) rather than trusting client-
    supplied values at verify time."""
    _require_configured()
    resp = requests.get(
        f"{_API_BASE}/orders/{order_id}",
        auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def verify_payment_signature(order_id: str, payment_id: str, signature: str) -> bool:
    _require_configured()
    payload = f"{order_id}|{payment_id}".encode()
    expected = hmac_lib.new(RAZORPAY_KEY_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return hmac_lib.compare_digest(expected, signature)
