"""
Outbound transactional email via Resend's HTTP API -- one POST call, no
SMTP setup. Same env-var-configured, provider-agnostic pattern as
tts.py/stt.py so swapping providers later needs no code change at call
sites, just different env vars.

RESEND_FROM defaults to Resend's own shared test domain, which works
immediately with no DNS setup but has lower deliverability/reputation
than a verified sending domain -- switch to something like
"PhoenixPrep <support@phoenixprep.in>" once phoenixprep.in (or a
subdomain) is verified in the Resend dashboard.
"""

import os

import requests

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "PhoenixPrep <onboarding@resend.dev>")


def send_email(*, to: str, subject: str, body_text: str) -> None:
    """Raises RuntimeError if RESEND_API_KEY isn't configured or the API
    call fails -- callers should catch this and surface a clean error
    rather than pretending the email went out."""
    if not RESEND_API_KEY:
        raise RuntimeError(
            "RESEND_API_KEY is not set. Configure it to enable sending email replies."
        )
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": RESEND_FROM,
            "to": [to],
            "subject": subject,
            "text": body_text,
        },
        timeout=20,
    )
    if not resp.ok:
        raise RuntimeError(f"{resp.status_code} error from Resend: {resp.text[:500]}")
