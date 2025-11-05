# server/tasks.py
"""
Lightweight async helpers that the views import:
- fire_and_forget(func, *args, **kwargs): run something off-thread
- post_payment_pipeline(order_id): placeholder pipeline you can expand
If you use Celery/RQ later, you can swap these with real tasks.
"""
from __future__ import annotations

import threading
import logging
from typing import Callable, Any

from django.utils import timezone

logger = logging.getLogger(__name__)

def fire_and_forget(func: Callable[..., Any], *args, **kwargs) -> None:
    """
    Run a callable in a background thread (non-blocking).
    """
    try:
        t = threading.Thread(target=_safe_wrapper, args=(func, args, kwargs), daemon=True)
        t.start()
    except Exception:
        logger.exception("fire_and_forget: failed to start thread for %s", getattr(func, "__name__", func))

def _safe_wrapper(func: Callable[..., Any], args, kwargs):
    try:
        func(*args, **kwargs)
    except Exception:
        logger.exception("fire_and_forget: error running %s", getattr(func, "__name__", func))

def post_payment_pipeline(order_id: int) -> None:
    """
    Placeholder for a post-payment pipeline. Expand as needed (e.g., analytics, fulfillment).
    Kept minimal so importing it in views doesn't explode if you don't wire a worker.
    """
    try:
        from .models import Order
        from .utils.invoices import generate_invoice_pdf
        from .utils.emails import send_payment_status_email

        order = Order.objects.filter(pk=order_id).first()
        if not order:
            return

        # Ensure invoice exists (idempotent)
        generate_invoice_pdf(order)

        # Email notifications (best-effort)
        try:
            locale = getattr(getattr(order.user, "profile", None), "language", "en") or "en"
            send_payment_status_email(order, status="paid", user_locale=locale)
        except Exception:
            logger.exception("post_payment_pipeline: email sending failed for order %s", order_id)
    except Exception:
        logger.exception("post_payment_pipeline: failed for order %s", order_id)
