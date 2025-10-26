# server/utils/emails.py
import re
import logging
from collections.abc import Iterable
from django.conf import settings
from django.core.mail import EmailMultiAlternatives, send_mail
from django.template.loader import render_to_string, TemplateDoesNotExist
from django.contrib.staticfiles import finders
from email.mime.image import MIMEImage

log = logging.getLogger(__name__)
CURRENCY = "€"

ADMIN_EMAIL = getattr(settings, "SUPPORT_EMAIL", "info@mindmines.eu")
DEFAULT_FROM = getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@example.com")
ORDERS_ADMIN_EMAIL = getattr(settings, "ORDERS_ADMIN_EMAIL", "info@mindmines.eu")
SITE_URL = getattr(settings, "FRONTEND_URL", "https://mindmines.eu").rstrip("/")
FRONTEND_ORDERS_URL = f"{SITE_URL}/account/orders"

MAX_ATTACH_BYTES = 14 * 1024 * 1024



def _fmt(amount): return f"{float(amount):.2f} {CURRENCY}"

def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()

def get_order_recipient(order) -> str | None:
    """
    Priority: explicit order.email -> guest_email -> order.user.email.
    """
    for cand in [
        getattr(order, "email", None),
        getattr(order, "guest_email", None),
        getattr(getattr(order, "user", None), "email", None),
    ]:
        if cand:
            return cand
    return None

def _send_bilingual(subject: str, to_email: str, en_html: str, de_html: str, text_fallback: str | None = None):
    if not to_email:
        return
    html = (
        "<div style='font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif'>"
        "<h3>ENGLISH</h3>"
        f"{en_html}"
        "<hr style='margin:24px 0'/>"
        "<h3>DEUTSCH</h3>"
        f"{de_html}"
        "</div>"
    )
    text = text_fallback or (
        "ENGLISH\n" + _strip_html(en_html) + "\n\nDEUTSCH\n" + _strip_html(de_html)
    )
    msg = EmailMultiAlternatives(
        subject=subject,
        body=text,
        from_email=getattr(settings, "EMAIL_HOST_USER", DEFAULT_FROM),
        to=[to_email],
    )
    msg.attach_alternative(html, "text/html")
    msg.send(fail_silently=not settings.DEBUG)

# ---------------- SEPA ----------------
def send_sepa_pending_email(order, user_locale: str = "en"):
    to_email = get_order_recipient(order)
    if not to_email:
        return

    ctx = {"order": order}
    try:
        html = render_to_string("emails/sepa_pending.html", ctx)
        msg = EmailMultiAlternatives(
            subject="SEPA pending / SEPA in Bearbeitung",
            body=_strip_html(html),
            from_email=getattr(settings, "EMAIL_HOST_USER", DEFAULT_FROM),
            to=[to_email],
        )
        msg.attach_alternative(html, "text/html")
        msg.send(fail_silently=not settings.DEBUG)
        return
    except TemplateDoesNotExist:
        pass

    en = (
        "<p>Your SEPA debit payment has been initiated. Confirmation may take 2–5 business days.</p>"
        "<p>We’ll email you once it completes.</p>"
    )
    de = (
        "<p>Ihre SEPA-Lastschrift wurde eingeleitet. Die Bestätigung kann 2–5 Werktage dauern.</p>"
        "<p>Wir informieren Sie, sobald die Zahlung abgeschlossen ist.</p>"
    )
    _send_bilingual("SEPA pending / SEPA in Bearbeitung", to_email, en, de)

# ------------- Return requests -------------
def send_return_request_emails(return_request):
    """
    Sends customer confirmation (bilingual) and a simple admin notification.
    """
    order = return_request.order
    customer = get_order_recipient(order)

    en = (
        f"<p>We received your return request for order <b>{order.number}</b>.</p>"
        f"<p>Message:</p><blockquote>{_strip_html(return_request.message)}</blockquote>"
    )
    de = (
        f"<p>Wir haben Ihre Rücksendeanfrage für die Bestellung <b>{order.number}</b> erhalten.</p>"
        f"<p>Nachricht:</p><blockquote>{_strip_html(return_request.message)}</blockquote>"
    )
    _send_bilingual("Return request received / Rücksendeanfrage erhalten", customer, en, de)

    # Admin
    try:
        admin_to = getattr(settings, "ADMIN_EMAIL", None) or DEFAULT_FROM
        if admin_to:
            send_mail(
                subject=f"[ADMIN] Return request {return_request.id} for order {order.number}",
                message=(
                    f"Order: {order.number}\n"
                    f"User: {getattr(order.user, 'id', '-')}\n"
                    f"Email: {customer}\n\n"
                    f"Message:\n{return_request.message}"
                ),
                from_email=getattr(settings, "EMAIL_HOST_USER", DEFAULT_FROM),
                recipient_list=[admin_to],
                fail_silently=not settings.DEBUG,
            )
    except Exception:
        log.exception("Admin return request email failed")

#---------------------- Cancel Request------------------
def send_cancel_request_emails(return_request):
    """
    Sends customer confirmation (bilingual) and a simple admin notification.
    """
    order = return_request.order
    customer = get_order_recipient(order)

    en = (
        f"<p>We received your cancellation request for order <b>{order.number}</b>.</p>"
        f"<p>Message:</p><blockquote>{_strip_html(return_request.message)}</blockquote>"
    )
    de = (
        f"<p>Wir haben Ihre Stornierungsanfrage für die Bestellung <b>{order.number}</b> erhalten.</p>"
        f"<p>Nachricht:</p><blockquote>{_strip_html(return_request.message)}</blockquote>"
    )
    _send_bilingual("Cancellation request received / Stornierungsanfrage erhalten", customer, en, de)

    try:
        admin_to = getattr(settings, "ADMIN_EMAIL", None) or DEFAULT_FROM
        if admin_to:
            send_mail(
                subject=f"[ADMIN] Cancel request {return_request.id} for order {order.number}",
                message=(
                    f"Order: {order.number}\n"
                    f"User: {getattr(order.user, 'id', '-')}\n"
                    f"Email: {customer}\n\n"
                    f"Message:\n{return_request.message}"
                ),
                from_email=getattr(settings, "EMAIL_HOST_USER", DEFAULT_FROM),
                recipient_list=[admin_to],
                fail_silently=not settings.DEBUG,
            )
    except Exception:
        log.exception("Admin cancellation request email failed")

# ------------- Shipping / Cancel updates -------------
def send_shipping_update(order, tracking: str | None = None):
    to_email = get_order_recipient(order)
    if not to_email:
        return

    ctx = {"order": order, "tracking": tracking}
    try:
        html = render_to_string("emails/shipping_update.html", ctx)
        msg = EmailMultiAlternatives(
            subject="Your order shipped / Ihre Bestellung wurde versendet",
            body=_strip_html(html),
            from_email=getattr(settings, "EMAIL_HOST_USER", DEFAULT_FROM),
            to=[to_email],
        )
        msg.attach_alternative(html, "text/html")
        msg.send(fail_silently=not settings.DEBUG)
        return
    except TemplateDoesNotExist:
        pass

    en = f"<p>Your order <b>{order.number}</b> is on the way.</p>" + (f"<p>Tracking: {tracking}</p>" if tracking else "")
    de = f"<p>Ihre Bestellung <b>{order.number}</b> ist unterwegs.</p>" + (f"<p>Sendungsverfolgung: {tracking}</p>" if tracking else "")
    _send_bilingual("Order shipped / Bestellung versendet", to_email, en, de)

# ------------------ ORDER CANCELLED ------------------
def send_cancellation_email(order):
    to_email = get_order_recipient(order)
    if not to_email:
        return

    ctx = {"order": order}
    try:
        html = render_to_string("emails/order_cancelled.html", ctx)
        msg = EmailMultiAlternatives(
            subject="Order cancelled / Bestellung storniert",
            body=_strip_html(html),
            from_email=getattr(settings, "EMAIL_HOST_USER", DEFAULT_FROM),
            to=[to_email],
        )
        msg.attach_alternative(html, "text/html")
        msg.send(fail_silently=not settings.DEBUG)
        return
    except TemplateDoesNotExist:
        pass

    en = f"<p>Your order <b>{order.number}</b> has been cancelled.</p>"
    de = f"<p>Ihre Bestellung <b>{order.number}</b> wurde storniert.</p>"
    _send_bilingual("Order cancelled / Bestellung storniert", to_email, en, de)

# ------------- Order confirmation -------------
def send_order_confirmation(order, user_locale: str = "en"):
    to_email = get_order_recipient(order)
    if not to_email:
        return

    ctx = {"order": order, "orders_url": FRONTEND_ORDERS_URL}
    try:
        html = render_to_string("emails/order_confirmation.html", ctx)
        msg = EmailMultiAlternatives(
            subject="Order confirmation / Bestellbestätigung",
            body=_strip_html(html),
            from_email=getattr(settings, "EMAIL_HOST_USER", DEFAULT_FROM),
            to=[to_email],
        )
        msg.attach_alternative(html, "text/html")
        msg.send(fail_silently=not settings.DEBUG)
        return
    except TemplateDoesNotExist:
        pass

    en = (
        f"<p>We’ve received your order <b>{order.number}</b>. We’ll email you once it ships.</p>"
        f"<p>You can view your order details anytime: {FRONTEND_ORDERS_URL}</p>"
    )
    de = (
        f"<p>Wir haben Ihre Bestellung <b>{order.number}</b> erhalten. Sobald sie versendet wurde, "
        "benachrichtigen wir Sie per E-Mail.</p>"
        f"<p>Bestelldetails: {FRONTEND_ORDERS_URL}</p>"
    )
    _send_bilingual("Order confirmation / Bestellbestätigung", to_email, en, de)


# ------------- Bank transfer -------------
def send_bank_transfer_instructions(order, bank: dict, user_locale: str = "en", due_hours: int = 48):
    to_email = get_order_recipient(order)
    if not to_email:
        return

    ctx = {"order": order, "bank": bank, "due_hours": due_hours}
    try:
        html = render_to_string("emails/bank_transfer_instructions.html", ctx)
        msg = EmailMultiAlternatives(
            subject="Bank transfer instructions / Überweisungsdetails",
            body=_strip_html(html),
            from_email=getattr(settings, "EMAIL_HOST_USER", DEFAULT_FROM),
            to=[to_email],
        )
        msg.attach_alternative(html, "text/html")
        msg.send(fail_silently=not settings.DEBUG)
        return
    except TemplateDoesNotExist:
        pass

    en = (
        f"<p>Thanks for your order <b>{order.number}</b>. Please transfer <b>{order.total_price:.2f} EUR</b> "
        f"within {due_hours} hours using the reference <b>{order.number}</b>.</p>"
        f"<p>IBAN: {bank.get('iban','')}<br/>BIC: {bank.get('bic','')}<br/>Beneficiary: {bank.get('account_name','')}</p>"
    )
    de = (
        f"<p>Vielen Dank für Ihre Bestellung <b>{order.number}</b>. Bitte überweisen Sie <b>{order.total_price:.2f} EUR</b> "
        f"innerhalb von {due_hours} Stunden mit dem Verwendungszweck <b>{order.number}</b>.</p>"
        f"<p>IBAN: {bank.get('iban','')}<br/>BIC: {bank.get('bic','')}<br/>Empfänger: {bank.get('account_name','')}</p>"
    )
    _send_bilingual("Bank transfer instructions / Überweisungsdetails", to_email, en, de)


# ------------- Installments -------------
def send_installments_started_email(order, months: int, amounts_eur: Iterable, dates: Iterable, user_locale: str = "en"):
    to_email = get_order_recipient(order)
    if not to_email:
        return

    en = (
        f"<p>Your {months}-month installment plan for order <b>{order.number}</b> has started.</p>"
        "<ul>" +
        "".join(f"<li>{i+1}. {a:.2f} € on {d.date().isoformat()}</li>" for i, (a, d) in enumerate(zip(amounts_eur, dates))) +
        "</ul>"
    )
    de = (
        f"<p>Ihr {months}-monatiger Ratenplan für die Bestellung <b>{order.number}</b> wurde gestartet.</p>"
        "<ul>" +
        "".join(f"<li>{i+1}. {a:.2f} € am {d.date().isoformat()}</li>" for i, (a, d) in enumerate(zip(amounts_eur, dates))) +
        "</ul>"
    )
    _send_bilingual("Installments started / Ratenzahlung gestartet", to_email, en, de)

# ------------- Invoice email -------------
def send_invoice_email(order, user_locale: str = "en"):
    """
    If you have templates under emails/invoice_email.html, they’ll be used,
    otherwise a simple bilingual fallback is sent.
    """
    to_email = get_order_recipient(order)
    if not to_email:
        return

    ctx = {"order": order}
    try:
        html = render_to_string("emails/invoice_email.html", ctx)
        msg = EmailMultiAlternatives(
            subject="Your invoice / Ihre Rechnung",
            body=_strip_html(html),
            from_email=getattr(settings, "EMAIL_HOST_USER", DEFAULT_FROM),
            to=[to_email],
        )
        msg.attach_alternative(html, "text/html")
        msg.send(fail_silently=not settings.DEBUG)
        return
    except TemplateDoesNotExist:
        pass

    en = (
        f"<p>Thanks for your purchase! Your invoice for order <b>{order.number}</b> is attached "
        "or available in your account.</p>"
        "<p>Need help? Just reply to this email.</p>"
    )
    de = (
        f"<p>Vielen Dank für Ihren Einkauf! Ihre Rechnung für die Bestellung <b>{order.number}</b> "
        "befindet sich im Anhang oder in Ihrem Konto.</p>"
        "<p>Brauchen Sie Hilfe? Antworten Sie einfach auf diese E-Mail.</p>"
    )
    _send_bilingual("Your invoice / Ihre Rechnung", to_email, en, de)


# ------------- Stock Alert -------------
def send_stock_alert_email(product, to_email: str, unsubscribe_url: str, user_locale: str = "en"):
    subject_en = f"Back in stock: {getattr(product, 'title_en', str(product))}"
    subject_de = f"Wieder verfügbar: {getattr(product, 'title_de', getattr(product, 'title_en', str(product)))}"
    subj = f"{subject_en} / {subject_de}"

    ctx = {
        "product": product,
        "product_url": f"{SITE_URL}/product/{product.id}",
        "unsubscribe_url": unsubscribe_url,
    }

    html_body = None
    try:
        html_body = render_to_string("emails/stock_alert.html", ctx)
    except TemplateDoesNotExist:
        pass

    text_fallback = (
        f"EN:\n{subject_en}\n\nCheck it out: {ctx['product_url']}\n"
        f"If you no longer want these alerts, unsubscribe: {unsubscribe_url}\n\n"
        f"— MindMines\n\n"
        f"DE:\n{subject_de}\n\nHier ansehen: {ctx['product_url']}\n"
        f"Wenn Sie keine Benachrichtigungen mehr erhalten möchten: {unsubscribe_url}\n\n"
        f"— MindMines\n"
    )

    msg = EmailMultiAlternatives(
        subject=subj,
        body=text_fallback,
        from_email=getattr(settings, "EMAIL_HOST_USER", DEFAULT_FROM),
        to=[to_email],
    )
    if html_body:
        msg.attach_alternative(html_body, "text/html")
    msg.send(fail_silently=not settings.DEBUG)

