"""
Outbound transactional email via Resend's HTTP API -- one POST call, no
SMTP setup. Same env-var-configured, provider-agnostic pattern as
tts.py/stt.py so swapping providers later needs no code change at call
sites, just different env vars.

RESEND_FROM defaults to phoenixprep.in now that it's a verified sending
domain in Resend -- override via the RESEND_FROM env var if that ever
needs to change (e.g. a different subdomain), no code change needed.
"""

import base64
import os

import requests

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "PhoenixPrep <support@phoenixprep.in>")


def send_email(*, to: str, subject: str, body_text: str, attachment: dict | None = None) -> None:
    """Raises RuntimeError if RESEND_API_KEY isn't configured or the API
    call fails -- callers should catch this and surface a clean error
    rather than pretending the email went out.

    `attachment`, if given: {"filename": str, "data": bytes}. Resend wants
    attachment content base64-encoded in the JSON body itself (no
    multipart upload needed on this side)."""
    if not RESEND_API_KEY:
        raise RuntimeError(
            "RESEND_API_KEY is not set. Configure it to enable sending email replies."
        )
    payload = {
        "from": RESEND_FROM,
        "to": [to],
        "subject": subject,
        "text": body_text,
    }
    if attachment:
        payload["attachments"] = [{
            "filename": attachment["filename"],
            "content": base64.b64encode(attachment["data"]).decode("ascii"),
        }]
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"{resp.status_code} error from Resend: {resp.text[:500]}")
