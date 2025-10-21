# server/tasks.py
"""
Lightweight task stubs. If you use Celery/RQ, replace the bodies with @shared_task tasks.
"""

from __future__ import annotations
from .utils.invoices import generate_invoice_pdf
from .utils.emails import send_invoice_email, send_order_confirmation
from django.utils import timezone
from .models import Order

def post_payment_pipeline(order_id: int):
    """
    Called after a successful payment (Stripe webhook / PayPal capture / admin mark paid).
    Keep idempotent.
    """
    try:
        order = Order.objects.get(pk=order_id)
    except Order.DoesNotExist:
        return

    # Generate/ensure invoice
    inv = generate_invoice_pdf(order)

    # Send invoice + confirmation (safe if sent twice — but we try to be gentle)
    try:
        send_invoice_email(order)
    except Exception:
        pass

    try:
        send_order_confirmation(order)
    except Exception:
        pass

    # You could add: notify warehouse, analytics, etc.
    return {"ok": True, "order": order.id, "invoice": getattr(inv, "number", None)}
