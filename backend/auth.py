"""
Clerk session-token verification.

Additive on top of the anonymous X-User-Id scheme the MVP started with:
if a request carries a valid Clerk session token (Authorization: Bearer
<token>), we trust it and use the real Clerk user id instead of the
anonymous header. Requests without a valid token fall back to X-User-Id
exactly as before -- signing in still isn't mandatory for practice mode,
only for anything that needs a durable identity (payments).
"""

import os
import uuid

import jwt
from jwt import PyJWKClient

CLERK_FRONTEND_API = os.environ.get("CLERK_FRONTEND_API", "daring-caiman-9439.clerk.accounts.dev")
_ISSUER = f"https://{CLERK_FRONTEND_API}"
_JWKS_URL = f"{_ISSUER}/.well-known/jwks.json"

_jwks_client = None


def _get_jwks_client():
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(_JWKS_URL)
    return _jwks_client


def verify_session_token(token: str) -> str | None:
    """Returns the Clerk user id (the `sub` claim) if the token is a valid,
    unexpired Clerk session JWT for this app; None otherwise (including any
    verification failure -- callers treat that the same as "not signed in",
    they don't need to distinguish why)."""
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=_ISSUER,
            options={"require": ["exp", "iat", "sub"]},
        )
        return payload["sub"]
    except Exception:
        return None


def resolve_user_id(authorization: str | None, x_user_id: str | None) -> str:
    """The single place request handlers go to find out who's calling.
    Prefers a verified Clerk identity; falls back to the anonymous
    X-User-Id header the frontend has always sent, and finally to a
    throwaway uuid if neither is present."""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        sub = verify_session_token(token)
        if sub:
            return f"clerk:{sub}"
    return x_user_id or str(uuid.uuid4())
