# server/signals.py
from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from .models import Order, Product, StockAlert, Profile
from .utils.invoices import generate_invoice_pdf
from .utils.emails import (
    send_payment_status_email,
    send_stock_alert_email,send_invoice_email
)

# --------------------------------------------------------------------
# Order status tracking and notifications
# --------------------------------------------------------------------

def _capture_prev_status(sender, instance: Order, **kwargs):
    """
    Stash previous status on instance._prev_status before save,
    so we can detect transitions (e.g., pending -> paid).
    """
    if instance.pk:
        try:
            prev = Order.objects.only("status").get(pk=instance.pk)
            instance._prev_status = prev.status
        except Order.DoesNotExist:
            instance._prev_status = None
    else:
        instance._prev_status = None

def create_or_update_user_profile(sender, instance, created, **kwargs):
    """
    Ensure Profile exists for every User.
    """
    if created:
        Profile.objects.create(user=instance)
    else:
        Profile.objects.get_or_create(user=instance)

def remember_old_status(sender, instance: Order, **kwargs):
    """
    Store old status on instance._old_status before saving. Compared in post_save.
    """
    if not instance.pk:
        instance._old_status = None
        instance._old_payment_status = None
        return
    try:
        old = Order.objects.get(pk=instance.pk)
        instance._old_status = old.status
        instance._old_payment_status = old.payment_status
    except Order.DoesNotExist:
        instance._old_status = None
        instance._old_payment_status = None

def handle_payment_status_notifications(sender, instance: Order, created, **kwargs):
    """Trigger customer notifications when payment status changes."""

    prev_payment_status = (getattr(instance, "_old_payment_status", None) or "").lower()
    prev_status = (getattr(instance, "_old_status", None) or "").lower()
    current_payment_status = (getattr(instance, "payment_status", "") or "").lower()
    current_status = (getattr(instance, "status", "") or "").lower()

    notify_status = None
    if current_payment_status and current_payment_status != prev_payment_status:
        notify_status = current_payment_status
    elif current_status != prev_status and current_status in {"processing", "paid", "failed"}:
        notify_status = current_status

    if not notify_status:
        return

    if notify_status == "processing" and getattr(instance, "confirmation_sent_at", None):
        return
    if notify_status == "failed" and getattr(instance, "failed_email_sent_at", None):
        return
    if notify_status == "paid" and getattr(instance, "invoice_sent_at", None):
        return

    user_locale = getattr(getattr(instance.user, "profile", None), "language", "en") or "en"

    if notify_status == "paid":
        # Avoid duplicate sends
        if getattr(instance, "invoice_sent_at", None):
            return
        try:
            instance.assign_unique_invoice_number()
        except Exception:
            pass
        try:
            generate_invoice_pdf(instance)
        except Exception:
            pass

        try:
            # This function sets invoice_sent_at internally on success
            send_invoice_email(instance, user_locale=user_locale)
        except Exception:
            pass
        return  # IMPORTANT: don't also send the generic payment-status email

    # For 'processing' / 'failed' keep your generic email
    try:
        send_payment_status_email(instance, status=notify_status, user_locale=user_locale)
    except Exception:
        pass

@receiver(post_save, sender=Order)
def ensure_order_number(sender, instance: Order, created, **kwargs):
    """
    Ensure an Order has a human-readable number like MM00000001.
    """
    if created and not getattr(instance, "number", None):
        try:
            instance.assign_unique_order_number()
        except AttributeError:
            instance.number = f"MM{instance.pk:08d}"
        type(instance).objects.filter(pk=instance.pk).update(number=instance.number)

def stamp_status_times(sender, instance: Order, created, **kwargs):
    """
    Add delivered_at / cancelled_at timestamps when status flips.
    """
    old = getattr(instance, "_old_status", None)
    updates = {}
    if instance.status == "delivered" and old != "delivered" and not getattr(instance, "delivered_at", None):
        updates["delivered_at"] = timezone.now()
    if instance.status == "cancelled" and old != "cancelled" and not getattr(instance, "cancelled_at", None):
        updates["cancelled_at"] = timezone.now()
    if updates:
        type(instance).objects.filter(pk=instance.pk).update(**updates)

# --------------------------------------------------------------------
# Product stock alert helpers
# --------------------------------------------------------------------

def _stash_old_qty(sender, instance: Product, **kwargs):
    """
    Before save, remember previous quantity so we can detect restock events.
    """
    if instance.pk:
        try:
            old = sender.objects.get(pk=instance.pk)
            instance._old_qty = int(getattr(old, "quantity", 0) or 0)
        except sender.DoesNotExist:
            instance._old_qty = None
    else:
        instance._old_qty = None

def _build_unsub_url(alert: StockAlert) -> str:
    base = getattr(settings, "FRONTEND_URL", "").rstrip("/")
    api_base = getattr(settings, "API_BASE_PATH", "/api").rstrip("/")
    # Keep parity with StockAlertUnsubscribeView
    return f"{base}{api_base}/stock-alerts/unsubscribe/{alert.unsubscribe_token}/"

def _notify_restock(sender, instance: Product, created, **kwargs):
    """
    After saving a Product:
    If stock goes from <=0 to >0, email everyone with active stock alerts for this product.
    """
    try:
        old_qty = getattr(instance, "_old_qty", None)
        new_qty = int(getattr(instance, "quantity", 0) or 0)
        if old_qty is None:
            # new product, do not notify on initial create
            return
        if (old_qty <= 0) and (new_qty > 0):
            alerts = StockAlert.objects.filter(product=instance, is_active=True).order_by("created_at")
            for alert in alerts:
                unsub = _build_unsub_url(alert)
                try:
                    send_stock_alert_email(instance, alert.email, unsub)
                except Exception:
                    continue
                alert.mark_notified()
    except Exception:
        # email or data issues should not crash request
        pass

# --------------------------------------------------------------------
# Public entry point called by apps.py
# --------------------------------------------------------------------
def connect_signals():
    """
    Attach all signal handlers with dispatch_uids to avoid duplicate connects.
    Called from ServerConfig.ready().
    """
    User = get_user_model()

    post_save.connect(
        create_or_update_user_profile,
        sender=User,
        dispatch_uid="server_user_profile_create_update",
    )

    pre_save.connect(
        remember_old_status,
        sender=Order,
        dispatch_uid="server_order_remember_old_status",
    )

    post_save.connect(
        handle_payment_status_notifications,
        sender=Order,
        dispatch_uid="server_order_paid_generate_mail",
    )

    post_save.connect(
        stamp_status_times,
        sender=Order,
        dispatch_uid="server_order_stamp_status_times",
    )

    pre_save.connect(
        _stash_old_qty,
        sender=Product,
        dispatch_uid="server_product_stash_old_qty",
    )

    post_save.connect(
        _notify_restock,
        sender=Product,
        dispatch_uid="server_product_notify_restock",
    )

    pre_save.connect(
        _capture_prev_status,
        sender=Order,
        dispatch_uid="server_capture_prev_status",
    )
