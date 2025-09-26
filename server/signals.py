
from django.db.models.signals import post_save, pre_save
from django.contrib.auth import get_user_model
from django.utils import timezone


from .utils.invoices import generate_invoice_pdf
from .utils.emails import send_invoice_email,send_order_confirmation




def create_or_update_user_profile(sender, instance, created, **kwargs):
    # Import here to avoid early app-registry import cycles
    from .models import Profile
    if created:
        Profile.objects.create(user=instance)
    else:
        # If profile missing for some reason, ensure it's present
        Profile.objects.get_or_create(user=instance)


def remember_old_status(sender, instance, **kwargs):
    if not instance.pk:
        instance._old_status = None
        return
    from .models import Order
    try:
        old = Order.objects.get(pk=instance.pk)
        instance._old_status = old.status
    except Order.DoesNotExist:
        instance._old_status = None

        


def on_paid_generate_and_mail(sender, instance, created, **kwargs):
    became_paid = instance.status == "paid" and getattr(instance, "_old_status", None) != "paid"
    if not became_paid:
        return

    # 1) generate/attach invoice
    generate_invoice_pdf(instance)

    # 2) send invoice email (+ optional order confirmation)
    send_invoice_email(instance)
    try:
        locale = getattr(getattr(instance.user, "profile", None), "language", "en") or "en"
        send_order_confirmation(instance, user_locale=locale)
    except Exception:
        # don't block if confirmation fails
        pass



def stamp_status_times(sender, instance, created, **kwargs):
    """Optional: stamp delivered_at / cancelled_at once when status changes."""
    old = getattr(instance, "_old_status", None)
    updates = {}
    # Guard with hasattr in case your model doesn't have these fields yet
    if hasattr(instance, "delivered_at") and instance.status == "delivered" and old != "delivered" and not getattr(instance, "delivered_at", None):
        updates["delivered_at"] = timezone.now()
    if hasattr(instance, "cancelled_at") and instance.status == "cancelled" and old != "cancelled" and not getattr(instance, "cancelled_at", None):
        updates["cancelled_at"] = timezone.now()
    if updates:
        type(instance).objects.filter(pk=instance.pk).update(**updates)


def connect_signals():
    """
    Call this once from apps.py -> ready().
    Doing it here ensures the app registry is loaded and avoids
    get_user_model() at import time.
    """
    User = get_user_model()
    from .models import Order

    post_save.connect(
        create_or_update_user_profile, sender=User,
        dispatch_uid="server_user_profile_create_update"
    )
    pre_save.connect(
        remember_old_status, sender=Order,
        dispatch_uid="server_order_remember_old_status"
    )
    post_save.connect(
        on_paid_generate_and_mail, sender=Order,
        dispatch_uid="server_order_paid_generate_mail"
    )
    post_save.connect(
        stamp_status_times, sender=Order,
        dispatch_uid="server_order_stamp_status_times"
    )