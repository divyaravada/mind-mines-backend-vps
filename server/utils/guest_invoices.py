# server/utils/guest_invoices.py
import json
from typing import Optional, Tuple
from django.urls import reverse
from urllib.parse import urlencode
from django.conf import settings

try:
    from itsdangerous import TimestampSigner
except ImportError:  # pragma: no cover - safety net if dependency changes
    TimestampSigner = None  # type: ignore

SIGNER_SALT = getattr(settings, "INVOICE_GUEST_SIGNER_SALT", "invoice-public-download-v1")
INVOICE_TOKEN_MAX_AGE = getattr(settings, "INVOICE_TOKEN_MAX_AGE", 60 * 60 * 2)


def _get_signer() -> TimestampSigner:
    if TimestampSigner is None:
        raise RuntimeError("TimestampSigner unavailable; ensure itsdangerous is installed.")    
    return TimestampSigner(settings.SECRET_KEY, salt=SIGNER_SALT)


def build_guest_invoice_token(order) -> Optional[str]:
    """
    Generate a signed token for guest invoice download links.
    Returns None when no invoice or PDF exists yet.
    """
    inv = getattr(order, "invoice", None)
    if not inv or not getattr(inv, "pdf", None):
        return None
    signer = _get_signer()
    payload = {"order_id": order.id, "invoice": inv.number}
    return signer.sign(json.dumps(payload))


def build_guest_invoice_download_url(order) -> Optional[str]:
    token = build_guest_invoice_token(order)
    if not token:
        return None
    # Build the server path and then prepend your public API base
    path = reverse("guest_invoice_download")  # -> "/api/orders/guest-invoice/"
    base = getattr(settings, "PUBLIC_API_BASE", "").rstrip("/")
    if base:
        return f"{base}{path}?{urlencode({'token': token})}"
    # Fallback: assume default base "/api"
    api_base = getattr(settings, "API_BASE_PATH", "/api").rstrip("/")
    return f"{api_base}{path}?{urlencode({'token': token})}"


def parse_guest_invoice_token(token: str) -> Tuple[Optional[int], Optional[str]]:
    signer = _get_signer()
    raw = signer.unsign(token, max_age=INVOICE_TOKEN_MAX_AGE)
    payload = json.loads(raw)
    return payload.get("order_id"), payload.get("invoice")
