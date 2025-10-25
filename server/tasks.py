# server/tasks.py
"""
Lightweight task stubs. If you use Celery/RQ, replace the bodies with @shared_task tasks.
"""

from __future__ import annotations

from .models import Order, Profile
from .utils.invoices import generate_invoice_pdf
from .utils.emails import send_invoice_email, send_order_confirmation,send_birthday_admin_digest, send_birthday_coupon_email
from django.utils import timezone
from django.db.models.functions import ExtractDay, ExtractMonth


def notify_today_birthdays():
    """
    Find all profiles with a birthday today (month/day) and notify admin.
    Use Europe/Berlin by default (your project already uses timezone aware dt).
    """
    today = timezone.localdate()
    qs = Profile.objects.filter(
        date_of_birth__isnull=False
    ).annotate(
        dob_month=ExtractMonth("date_of_birth"),
        dob_day=ExtractDay("date_of_birth"),
    ).filter(dob_month=today.month, dob_day=today.day)

    profiles_today = list(qs.select_related("user"))
    if profiles_today:
        send_birthday_admin_digest(profiles_today)
    return {"count": len(profiles_today)}

def send_user_birthday_coupon(user_id: int, coupon_code: str):
    """
    Helper to email a specific user a coupon (e.g. triggered from admin or API).
    """
    from .models import User
    try:
        u = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return {"ok": False, "error": "user_not_found"}
    send_birthday_coupon_email(u, coupon_code)
    return {"ok": True}

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


