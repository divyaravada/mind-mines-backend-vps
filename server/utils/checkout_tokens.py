# server/utils/checkout_tokens.py
from __future__ import annotations

import typing as _t
from django.conf import settings
from django.core import signing

# Keep a separate salt so keys don’t collide with other signers
_SALT = getattr(settings, "CHECKOUT_TOKEN_SALT", "checkout-token-v1")

def pack_checkout_payload(payload: _t.Mapping) -> str:
    """
    Sign + compress a small mapping (email, items, shipping, coupon_code, pm alias).
    Returns a URL-safe string. Keep payload JSON-ish (dicts, lists, str, int, Decimal as str).
    """
    # signing.dumps serializes + signs; compress=True keeps it short if large
    return signing.dumps(payload, salt=_SALT, compress=True)

def unpack_checkout_payload(token: str) -> dict:
    """
    Verify signature and return the original mapping.
    Raises BadSignature/SignatureExpired if invalid.
    """
    return signing.loads(token, salt=_SALT)
