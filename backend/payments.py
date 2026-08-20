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

PRO_PRICE_PAISE = 19900  # ₹199.00, Razorpay amounts are in the smallest currency unit

_API_BASE = "https://api.razorpay.com/v1"


class PaymentsNotConfigured(Exception):
    pass


def _require_configured():
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise PaymentsNotConfigured(
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set -- payments aren't configured yet."
        )


def create_order(user_id: str) -> dict:
    _require_configured()
    resp = requests.post(
        f"{_API_BASE}/orders",
        auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
        json={
            "amount": PRO_PRICE_PAISE,
            "currency": "INR",
            "notes": {"user_id": user_id, "plan": "pro_monthly"},
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
    }


def verify_payment_signature(order_id: str, payment_id: str, signature: str) -> bool:
    _require_configured()
    payload = f"{order_id}|{payment_id}".encode()
    expected = hmac_lib.new(RAZORPAY_KEY_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return hmac_lib.compare_digest(expected, signature)
