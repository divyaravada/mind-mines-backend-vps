# server/utils/emails.py
import logging
import re
from collections.abc import Iterable
from email.mime.image import MIMEImage

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.mail import EmailMultiAlternatives, send_mail
from django.template.loader import render_to_string, TemplateDoesNotExist

log = logging.getLogger(__name__)

CURRENCY = "€"
SITE_NAME = getattr(settings, "SITE_NAME", "MindMines")

ADMIN_EMAIL = getattr(settings, "ADMIN_EMAIL", "info@mindmines.eu")
ORDERS_ADMIN_EMAIL = getattr(settings, "ORDERS_ADMIN_EMAIL", ADMIN_EMAIL)
DEFAULT_FROM = getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@example.com")

MAX_ATTACH_BYTES = 14 * 1024 * 1024



def _fmt(amount) -> str:
    try:
        return f"{float(amount):.2f} {CURRENCY}"
    except Exception:
        return f"{amount} {CURRENCY}"


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


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


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


def _attach_inline_logo(email_msg: EmailMultiAlternatives) -> str | None:
    """
    Attach a static logo inline and return the CID (without <>).
    Template can use <img src="cid:{{ logo_cid }}">
    """
    try:
        path = (
            finders.find("email/logo.png")
            or finders.find("images/logo.png")
            or finders.find("logo.png")
        )
        if not path:
            return None
        with open(path, "rb") as f:
            img = MIMEImage(f.read())
        cid = "brandlogo"
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename="logo.png")
        email_msg.attach(img)
        return cid
    except Exception:
        return None

# ---------- Status/Flow Emails ----------

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

    # Admin notify
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
        log.exception("Admin cancel email failed")


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
        log.exception("Admin return email failed")


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


def send_order_confirmation(order, user_locale: str = "en"):
    to_email = get_order_recipient(order)
    if not to_email:
        return

    ctx = {"order": order}
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
        "<p>You can view your order details anytime.</p>"
    )
    de = (
        f"<p>Wir haben Ihre Bestellung <b>{order.number}</b> erhalten. Sobald sie versendet wurde, "
        "benachrichtigen wir Sie per E-Mail.</p>"
        "<p>Sie können Ihre Bestelldetails jederzeit einsehen.</p>"
    )
    _send_bilingual("Order confirmation / Bestellbestätigung", to_email, en, de)


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


def send_invoice_email(order, user_locale: str = "en"):
    """
    Sends invoice email (bilingual). If you have a template "emails/invoice_email.html",
    it's used; otherwise a safe fallback is sent.
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


# ---------- Stock Alerts ----------

def send_stock_alert_email(product, to_email: str, unsubscribe_url: str, user_locale: str = "en"):
    """
    Sends a bilingual stock notification email to a subscriber.
    Falls back to plain text if the HTML template is missing.
    """
    subject = {
        "en": f"Back in stock: {getattr(product, 'title_en', str(product))}",
        "de": f"Wieder verfügbar: {getattr(product, 'title_de', getattr(product, 'title_en', str(product)))}",
    }
    subj = f"{subject['en']} / {subject['de']}"

    ctx = {
        "product": product,
        "product_url": getattr(settings, "FRONTEND_URL", "").rstrip("/") + f"/product/{product.id}",
        "unsubscribe_url": unsubscribe_url,
    }

    text_fallback = (
        f"EN:\n{subject['en']}\n\nCheck it out: {ctx['product_url']}\n"
        f"If you no longer want these alerts, unsubscribe: {unsubscribe_url}\n\n"
        f"— {SITE_NAME}\n\n"
        f"DE:\n{subject['de']}\n\nHier ansehen: {ctx['product_url']}\n"
        f"Wenn Sie keine Benachrichtigungen mehr erhalten möchten: {unsubscribe_url}\n\n"
        f"— {SITE_NAME}\n"
    )

    msg = EmailMultiAlternatives(
        subject=subj,
        body=text_fallback,
        from_email=DEFAULT_FROM,
        to=[to_email],
    )

    try:
        html_body = render_to_string("emails/stock_alert.html", ctx)
        msg.attach_alternative(html_body, "text/html")
    except TemplateDoesNotExist:
        pass

    msg.send(fail_silently=not settings.DEBUG)

# ---------- Birthday Emails ----------
def send_birthday_admin_digest(profiles_today):
    """
    Email admin a list of users who have a birthday today.
    Admin can then create/send coupons as desired.
    """
    if not profiles_today:
        return
    admin_to = getattr(settings, "ADMIN_EMAIL", None) or getattr(settings, "DEFAULT_FROM_EMAIL", None)
    if not admin_to:
        return

    lines = []
    for p in profiles_today:
        u = p.user
        lines.append(f"- {u.email} | {p.full_name or (u.username or '')} | DOB: {p.date_of_birth.isoformat()}")

    subject = "🎂 Birthday notification — users with birthdays today"
    body = "The following users have birthdays today:\n\n" + "\n".join(lines)
    EmailMultiAlternatives(
        subject=subject,
        body=body,
        from_email=getattr(settings, "EMAIL_HOST_USER", settings.DEFAULT_FROM_EMAIL),
        to=[admin_to],
    ).send(fail_silently=not settings.DEBUG)


def send_birthday_coupon_email(user, coupon_code: str):
    """
    Send a friendly bilingual birthday coupon email to a user.
    """
    to_email = getattr(user, "email", None)
    if not to_email:
        return
    subj = "Happy Birthday! 🎉 / Alles Gute zum Geburtstag! 🎉"
    en = (
        f"<p>Happy Birthday from {SITE_NAME}! Use your coupon code "
        f"<b>{coupon_code}</b> at checkout to enjoy a special discount.</p>"
        "<p>We hope you have a wonderful day!</p>"
    )
    de = (
        f"<p>Alles Gute zum Geburtstag von {SITE_NAME}! Verwenden Sie den Gutscheincode "
        f"<b>{coupon_code}</b> beim Checkout für einen besonderen Rabatt.</p>"
        "<p>Wir wünschen einen wundervollen Tag!</p>"
    )

#----------------- Order Context & Invoice Attachment ------------------
def build_order_context(order):
    # Adjust field names to your models
    items = []
    subtotal = 0
    for li in order.items.all():  # e.g. OrderItem related_name="items"
        line_total = float(li.unit_price) * li.quantity
        subtotal += line_total
        items.append({
            "name": li.product_name or getattr(li.product, "title_en", "Item"),
            "qty": li.quantity,
            "unit": _fmt(float(li.unit_price)),
            "total_display": _fmt(line_total),
        })

    shipping = float(getattr(order, "shipping_fee", 0) or 0)
    total = subtotal + shipping

    address = getattr(order, "shipping_address", None)
    addr = {
        "full_name": getattr(address, "full_name", "") or "",
        "country": getattr(address, "country", "") or "",
        "street_name": getattr(address, "street_name", "") or "",
        "house_number": getattr(address, "house_number", "") or "",
        "apartment": getattr(address, "apartment", "") or "",
        "postal_code": getattr(address, "postal_code", getattr(address, "pincode", "")) or "",
        "city": getattr(address, "city", getattr(address, "state", "")) or "",
        "phone": getattr(address, "phone", "") or "",
    }
   
    
    return {
        "order": order,
        "items": items,
        "address": addr,
        "totals": {
            "subtotal_display": _fmt(subtotal),
            "shipping_display": "Free" if shipping == 0 else _fmt(shipping),
            "total_display": _fmt(total),
        },


    }
  

def attach_invoice_if_available(email: EmailMultiAlternatives, order):
    pdf_path = getattr(order, "invoice_pdf_path", None) or getattr(order, "invoice_pdf", None)
    # pdf_path could be a storage path or FileField; handle both
    try:
        if hasattr(pdf_path, "path"):  # FileField
            with open(pdf_path.path, "rb") as f:
                email.attach(filename=f"Invoice-{order.number}.pdf", content=f.read(), mimetype="application/pdf")
        elif isinstance(pdf_path, str):
            with open(pdf_path, "rb") as f:
                email.attach(filename=f"Invoice-{order.number}.pdf", content=f.read(), mimetype="application/pdf")
    except Exception:
        # Don’t block email if invoice isn’t ready; you can email it separately later
        pass


# ------------------ RETURN / CANCELLATION REQUEST ------------------

ADMIN_ORDERS_EMAIL = getattr(settings, "ORDERS_ADMIN_EMAIL", "info@mindmines.eu")

def notify_admin(subject, body, attachments=None, fail_silently=True):
    try:
        msg = EmailMultiAlternatives(subject, body, settings.DEFAULT_FROM_EMAIL, [ADMIN_ORDERS_EMAIL])
        if attachments:
            for (fname, content, mimetype) in attachments:
                msg.attach(fname, content, mimetype)
        msg.send(fail_silently=fail_silently)
    except Exception:
        import logging; logging.getLogger(__name__).exception("notify_admin failed")








