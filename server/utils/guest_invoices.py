import json
from typing import Optional, Tuple, Any, Protocol, runtime_checkable, Mapping, cast
from urllib.parse import urlencode, urljoin
from itsdangerous import BadSignature, SignatureExpired

from django.conf import settings
from django.urls import reverse

# --- Signing types ------------------------------------------------------------

@runtime_checkable
class _SignerProto(Protocol):
    def sign(self, value: str) -> str: ...
    def unsign(self, signed_value: str, *, max_age: Optional[int] = ...) -> str: ...

try:
    # If available at runtime, great; for typing we rely on the protocol above.
    from itsdangerous import TimestampSigner as _TimestampSigner  # type: ignore
except Exception:  # pragma: no cover
    _TimestampSigner = None  # type: ignore[assignment]

SIGNER_SALT: str = getattr(settings, "INVOICE_GUEST_SIGNER_SALT", "invoice-public-download-v1")
INVOICE_TOKEN_MAX_AGE: int = int(getattr(settings, "INVOICE_TOKEN_MAX_AGE", 60 * 60 * 24 * 360)) # default 1 year 


def _get_signer() -> _SignerProto:
    """
    Return a signer compatible with our protocol or raise at runtime
    if itsdangerous isn't installed.
    """
    if _TimestampSigner is None:
        raise RuntimeError("TimestampSigner unavailable; install 'itsdangerous'.")
    # Create and return an object that matches _SignerProto
    return _TimestampSigner(settings.SECRET_KEY, salt=SIGNER_SALT)  # type: ignore[no-any-return]


def _absolute_url(base: str, path: str) -> str:
    # joins cleanly (no double /api, no missing slashes)
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def build_guest_invoice_token(order: Any) -> Optional[str]:
    """
    Generate a signed token for guest invoice download links.
    Returns None when no invoice or PDF exists yet.
    """
    inv = getattr(order, "invoice", None)
    if not inv or not getattr(inv, "pdf", None):
        return None

    signer = _get_signer()
    payload = {"order_id": getattr(order, "id", None), "invoice": getattr(inv, "number", "")}
    data = json.dumps(payload)

    signed = signer.sign(data)
    # itsdangerous usually returns str; guard just in case:
    return signed.decode("utf-8") if isinstance(signed, (bytes, bytearray)) else signed


def build_guest_invoice_download_url(order: Any) -> Optional[str]:
    token = build_guest_invoice_token(order)
    if not token:
        return None

    path = reverse("guest_invoice_download")  # e.g. "/api/orders/guest-invoice/"
    qs = urlencode({"token": token})

    # Prefer domain-only origin (recommended)
    origin = str(getattr(settings, "FRONTEND_URL", "")).strip()  # e.g. "https://mindmines.eu"
    if origin:
        return f"{_absolute_url(origin, path)}?{qs}"

    # Or, if you keep an API base that may already include /api
    api_base = str(getattr(settings, "FRONTEND_URL_API", "")).strip()  # e.g. "https://mindmines.eu/api"
    if api_base:
        return f"{_absolute_url(api_base, path)}?{qs}"

    # Final fallback (relative absolute path on current host)
    return f"{path}?{qs}"


def parse_guest_invoice_token(token: str) -> Tuple[Optional[int], Optional[str]]:
    signer = _get_signer()

    try:
        raw = signer.unsign(token, max_age=INVOICE_TOKEN_MAX_AGE)
    except SignatureExpired as exc:
        # Re-raise a custom exception or signal "expired" up to the view
        raise
    except BadSignature as exc:
        raise
    raw_str = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw

    payload = cast(Mapping[str, Any], json.loads(raw_str))
    order_id = payload.get("order_id")
    invoice_no = payload.get("invoice")

    # Normalize to expected Optional[int], Optional[str]
    return (int(order_id) if isinstance(order_id, int) else None,
            str(invoice_no) if isinstance(invoice_no, str) else None)
