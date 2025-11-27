# server/utils/emails.py
import re
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional, Sequence, Tuple
from django.conf import settings
from django.core.mail import EmailMultiAlternatives, send_mail, get_connection
from django.template.loader import render_to_string, TemplateDoesNotExist
from django.utils import timezone
from .guest_invoices import build_guest_invoice_download_url
from .invoices import generate_invoice_pdf

logger = logging.getLogger(__name__)
CURRENCY = "€"

DEFAULT_FROM = getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@example.com")
SITE_URL = getattr(settings, "FRONTEND_URL", "https://mindmines.eu").rstrip("/")
FRONTEND_ORDERS_URL = f"{SITE_URL}/account/orders"

BRAND_NAME = getattr(settings, "EMAIL_BRAND_NAME", "MindMines")
EMAIL_FOOTER_TEXT_EN = getattr(
    settings,
    "EMAIL_FOOTER_TEXT_EN",
    "You received this email because you interacted with MindMines (e.g. placed an order or sent a request).",
)
EMAIL_FOOTER_TEXT_DE = getattr(
    settings,
    "EMAIL_FOOTER_TEXT_DE",
    "Sie erhalten diese E-Mail, weil Sie mit MindMines interagiert haben (z. B. eine Bestellung aufgegeben oder eine Anfrage gesendet haben).",
)
EMAIL_LOGO_PATH = getattr(settings, "EMAIL_LOGO_PATH", "emails/logo.png")


def _email_logo_url() -> str:
    """
    Build an absolute URL to the email logo.

    By default this assumes your logo lives at ``STATIC_URL/emails/logo.png``.
    If you want something else (e.g. a CDN URL), set ``EMAIL_LOGO_PATH`` or
    ``EMAIL_LOGO_URL`` in ``settings.py``.
    """
    explicit = getattr(settings, "EMAIL_LOGO_URL", "").strip()
    if explicit:
        return explicit

    site_url = getattr(settings, "SITE_URL", SITE_URL).rstrip("/")
    static_url = getattr(settings, "STATIC_URL", "/static/").rstrip("/")

    logo_rel = EMAIL_LOGO_PATH.lstrip("/")
    if site_url:
        return f"{site_url}{static_url}/{logo_rel}"
    # Fallback: relative URL – still works in many email clients
    return f"{static_url}/{logo_rel}"


def order_recipient_email(order) -> Optional[str]:
    """Recipient for order emails (guest + authed)."""
    return (
        getattr(order, "email", None)
        or getattr(getattr(order, "user", None), "email", None)
    )

def _money(v: Decimal, currency: str = "EUR") -> str:
    try:
        return f"{Decimal(v):.2f} {currency}"
    except Exception:
        return f"{v} {currency}"

def _from_email() -> str:
    return getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@example.com")

def _bcc_defaults() -> Sequence[str]:
    return getattr(settings, "ORDER_EMAIL_BCC", [])

def _attach_invoice_if_available(order, email_msg: EmailMultiAlternatives):
    """Attach invoice PDF if present (Invoice.pdf or legacy attribute)."""
    try:
        inv = getattr(order, "invoice", None)
        if inv and getattr(inv, "pdf", None) and inv.pdf:
            name = f"{getattr(inv, 'number', f'invoice_{order.id}')}.pdf"
            # ensure fresh file handle each send
            try:
                email_msg.attach(name, inv.pdf.open("rb").read(), "application/pdf")
            finally:
                try:
                    inv.pdf.close()
                except Exception:
                    pass
            return

        # Legacy: order.invoice_pdf (FileField) or raw bytes
        legacy = getattr(order, "invoice_pdf", None)
        if legacy:
            try:
                # FileField
                f = legacy.open("rb")
            except Exception:
                # raw bytes
                if isinstance(legacy, (bytes, bytearray)):
                    email_msg.attach(
                        f"invoice_{order.id}.pdf", legacy, "application/pdf"
                    )
                    return
                raise

            email_msg.attach(
                f.name.split("/")[-1], f.read(), "application/pdf"
            )
    except Exception:
        logger.exception("Failed reading legacy invoice pdf for order %s", order.id)

@dataclass
class EmailBody:
    subject: str
    text: str
    html: str

def _tr(locale: str, key: str, **vars) -> str:
    """Tiny inline i18n. locale in {'en','de'}."""
    loc = (locale or "en").split("-")[0].lower()
    t = {
        "subject_sepa_pending": {
            "en": "Your MindMines order is being processed (SEPA)",
            "de": "Ihre MindMines-Bestellung wird verarbeitet (SEPA)",
        },
        "subject_invoice": {
            "en": "Your MindMines invoice for Order {num}",
            "de": "Ihre MindMines-Rechnung für Bestellung {num}",
        },
        "subject_admin_order_paid": {
            "en": "Order {num} paid – invoice sent",
            "de": "Bestellung {num} bezahlt – Rechnung gesendet",
        },
        "subject_admin_new_order": {
            "en": "New order {num}",
            "de": "Neue Bestellung {num}",
        },
        "subject_order_confirm": {
            "en": "Order {num} confirmed",
            "de": "Bestellung {num} bestätigt",
        },
        "subject_installments_started": {
            "en": "Installment plan started for Order {num}",
            "de": "Ratenplan gestartet für Bestellung {num}",
        },
        "subject_payment_status": {
            "en": "Payment status update for Order {num}",
            "de": "Zahlungsstatus-Update für Bestellung {num}",
        },
        "salute": {
            "en": "Dear customer",
            "de": "Liebe Kundin, lieber Kunde",
        },
        "thanks": {
            "en": "Best regards",
            "de": "Viele Grüße",
        },
        "brand": {
            "en": "MindMines",
            "de": "MindMines",
        },
        "we_will_email_updates": {
            "en": "We will email you as soon as there is an update.",
            "de": "Wir informieren Sie per E-Mail, sobald es ein Update gibt.",
        },
        "sepa_pending_intro": {
            "en": "We have received your SEPA payment instruction. Your bank will process the payment in the next days.",
            "de": "Wir haben Ihre SEPA-Zahlungsanweisung erhalten. Ihre Bank wird die Zahlung in den nächsten Tagen bearbeiten.",
        },
        "order_confirm_intro": {
            "en": "Thank you for your order! Here is a summary:",
            "de": "Vielen Dank für Ihre Bestellung! Hier eine Zusammenfassung:",
        },
        "payment_status_intro": {
            "en": "Here is an update on the payment status of your order.",
            "de": "Hier ist ein Update zum Zahlungsstatus Ihrer Bestellung.",
        },
    }
    try:
        value = t[key][loc]
    except Exception:
        value = t.get(key, {}).get("en", key)
    try:
        return value.format(**vars)
    except Exception:
        return value


def _html_to_text(html: str) -> str:
    # crude but good enough for transactional emails
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"</p\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _order_header_html(order) -> str:
    try:
        num = getattr(order, "number", None) or f"#{order.id}"
        total_val = getattr(order, "total_price", Decimal("0.00"))
        total = _money(total_val)
        created = getattr(order, "created_at", None) or getattr(
            order, "created", None
        )
        created_str = created.strftime("%Y-%m-%d %H:%M") if created else ""
        return (
            f"<p><strong>Order:</strong> {num}<br />"
            f"<strong>Total:</strong> {total}<br />"
            f"<strong>Date:</strong> {created_str}</p>"
        )
    except Exception:
        logger.exception(
            "Failed to render order header for order %s", getattr(order, "id", "?")
        )
        return ""


def _line_items_html(order) -> str:
    try:
        items = getattr(order, "items", None) or getattr(
            order, "order_items", None
        ) or []
        rows = []
        for it in items.all() if hasattr(items, "all") else items:
            title = getattr(it, "product_title", None) or getattr(
                getattr(it, "product", None), "title_en", ""
            )
            qty = getattr(it, "quantity", 1)
            unit_val = getattr(it, "unit_price", None)
            if unit_val is None:
                unit_val = getattr(it, "unit_price_gross", Decimal("0.00"))
            unit = _money(unit_val)
            rows.append(f"<li>{title} &times; {qty} — {unit}</li>")
        return "<ul>" + "".join(rows) + "</ul>" if rows else ""
    except Exception:
        logger.exception(
            "Failed to render line items for order %s", getattr(order, "id", "?")
        )
        return ""


def _send_email(
    subject: str,
    text: str,
    html: str,
    to_email: str,
    bcc: Optional[Iterable[str]] = None,
    attachments: Optional[Iterable[Tuple[str, bytes, str]]] = None,
) -> bool:
    if not to_email:
        logger.warning("No recipient provided; skipping email: %s", subject)
        return False
    try:
        with get_connection() as conn:
            msg = EmailMultiAlternatives(
                subject=subject,
                body=text,
                from_email=_from_email(),
                to=[to_email],
                bcc=list(bcc or _bcc_defaults()),
                connection=conn,
            )
            msg.attach_alternative(html, "text/html")
            if attachments:
                for name, content, mimetype in attachments:
                    msg.attach(name, content, mimetype)
            msg.send()
        return True
    except Exception:
        logger.exception(
            "Failed to send email to %s (subject=%s)", to_email, subject
        )
        return False


def _base_wrapper(
    subject_en: str, subject_de: str, body_en_html: str, body_de_html: str
) -> EmailBody:
    """
    Wrap two language-specific HTML fragments in a branded email shell.

    - Shows EN + DE one under the other.
    - Adds MindMines logo and a light card-style container.
    """
    subject = f"{subject_en} / {subject_de}"
    text_en = _html_to_text(body_en_html)
    text_de = _html_to_text(body_de_html)
    text = f"{text_en}\n\n{'-'*40}\n\n{text_de}"

    logo_url = _email_logo_url()
    brand = BRAND_NAME or _tr("en", "brand")
    footer_en = EMAIL_FOOTER_TEXT_EN
    footer_de = EMAIL_FOOTER_TEXT_DE
    site_url = SITE_URL

    logo_html = (
        f'<img src="{logo_url}" alt="{brand}" style="height:32px;display:block;" />'
        if logo_url
        else ""
    )

    html = f"""<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="color-scheme" content="light only" />
    <meta name="supported-color-schemes" content="light only" />
    <title>{subject}</title>
  </head>
  <body style="margin:0;padding:24px;background-color:#f5f5f5;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;line-height:1.5;color:#111;">
    <div style="max-width:640px;margin:0 auto;background:#ffffff;border-radius:12px;border:1px solid #e5e5e5;overflow:hidden;">
      <div style="display:flex;align-items:center;gap:12px;padding:16px 20px;border-bottom:1px solid #f0f0f0;">
        {logo_html}
        <span style="font-size:18px;font-weight:600;letter-spacing:0.02em;">{brand}</span>
      </div>
      <div style="padding:20px;">
        <h3 style="margin:0 0 12px;font-size:16px;color:#444;">English</h3>
        {body_en_html}
        <hr style="margin:24px 0;border:none;border-top:1px solid #eee;" />
        <h3 style="margin:0 0 12px;font-size:16px;color:#444;">Deutsch</h3>
        {body_de_html}
      </div>
      <div style="padding:12px 20px;border-top:1px solid #f0f0f0;background:#fafafa;font-size:12px;color:#777;text-align:center;">
        <div style="margin-bottom:4px;">{footer_en}</div>
        <div style="margin-bottom:4px;">{footer_de}</div>
        <div style="opacity:0.8;">{site_url}</div>
      </div>
    </div>
  </body>
</html>"""
    return EmailBody(subject=subject, text=text, html=html)


def _send_bilingual(
    subject: str,
    to_email: str,
    en_html: str,
    de_html: str,
    text_fallback: str | None = None,
):
    """
    Convenience helper for short EN/DE notification emails that don't need
    their own templates (e.g. return / cancel requests).

    The subject is expected to be "EN subject / DE Betreff", but if that
    pattern is not found we simply reuse the same subject for both parts.
    """
    if not to_email:
        return False

    if " / " in subject:
        subject_en, subject_de = [
            part.strip() for part in subject.split(" / ", 1)
        ]
    else:
        subject_en = subject_de = subject

    mail = _base_wrapper(subject_en, subject_de, en_html, de_html)
    text = text_fallback or mail.text

    try:
        msg = EmailMultiAlternatives(
            subject=mail.subject,
            body=text,
            from_email=getattr(settings, "EMAIL_HOST_USER", DEFAULT_FROM),
            to=[to_email],
        )
        msg.attach_alternative(mail.html, "text/html")
        msg.send(fail_silently=not settings.DEBUG)
        return True
    except Exception:
        logger.exception(
            "Failed to send bilingual email to %s (subject=%s)",
            to_email,
            subject,
        )
        return False


# ---------------- SEPA ----------------
def send_sepa_pending_email(
    order, user_locale: str = "en", to: Optional[str] = None
) -> bool:
    to_email = to or order_recipient_email(order)
    if not to_email:
        logger.warning(
            "Skipping SEPA pending email for order %s – no recipient",
            getattr(order, "id", None),
        )
        return False
    subject_en = _tr("en", "subject_sepa_pending")
    subject_de = _tr("de", "subject_sepa_pending")
    body_en = f"""
      <p>{_tr('en','salute')},</p>
      <p>{_tr('en','sepa_pending_intro')}</p>
      {_order_header_html(order)}
      {_line_items_html(order)}
      <p>{_tr('en','we_will_email_updates')}</p>
      <p>{_tr('en','thanks')}<br />{_tr('en','brand')}</p>
    """
    body_de = f"""
      <p>{_tr('de','salute')},</p>
      <p>{_tr('de','sepa_pending_intro')}</p>
      {_order_header_html(order)}
      {_line_items_html(order)}
      <p>{_tr('de','we_will_email_updates')}</p>
      <p>{_tr('de','thanks')}<br />{_tr('de','brand')}</p>
    """
    mail = _base_wrapper(subject_en, subject_de, body_en, body_de)
    return _send_email(mail.subject, mail.text, mail.html, to_email)

# ------------- Admin: order paid alert -------------
def send_admin_order_paid_email(order) -> bool:
    """
    Notify the admin when an order is marked as PAID (invoice email sent).
    """
    try:
        admin_to = getattr(settings, "ADMIN_EMAIL", None) or DEFAULT_FROM
        if not admin_to:
            logger.warning(
                "ADMIN_EMAIL not configured; skipping admin order paid email for order %s",
                getattr(order, "id", None),
            )
            return False

        num = getattr(order, "number", None) or f"#{order.id}"
        total_val = getattr(order, "total_price", Decimal("0.00"))
        total = _money(total_val)

        subject_en = _tr("en", "subject_admin_order_paid", num=num)
        subject_de = _tr("de", "subject_admin_order_paid", num=num)
        subject = f"{subject_en} / {subject_de}"

        text = (
            f"Order {num} has been marked as PAID.\n"
            f"Total: {total}\n"
            f"Customer: {order_recipient_email(order) or '-'}\n"
        )

        html = f"""
          <p><strong>Order paid.</strong></p>
          {_order_header_html(order)}
          {_line_items_html(order)}
          <p>Customer email: {order_recipient_email(order) or '-'}</p>
        """
        return _send_email(subject, _html_to_text(html), html, admin_to)
    except Exception:
        logger.exception(
            "Failed to send admin order paid email for order %s",
            getattr(order, "id", None),
        )
        return False

def send_admin_new_order_email(order) -> bool:
    """
    Notify the admin whenever a new order is created.
    """
    try:
        admin_to = getattr(settings, "ADMIN_EMAIL", None) or DEFAULT_FROM
        if not admin_to:
            logger.warning(
                "ADMIN_EMAIL not configured; skipping admin new order email for order %s",
                getattr(order, "id", None),
            )
            return False

        num = getattr(order, "number", None) or f"#{order.id}"
        total_val = getattr(order, "total_price", Decimal("0.00"))
        total = _money(total_val)

        customer_email = (order_recipient_email(order) or "-").strip() or "-"
        subject_en = _tr("en", "subject_admin_new_order", num=num)
        subject_de = _tr("de", "subject_admin_new_order", num=num)
        subject = f"{subject_en} / {subject_de}"

        text = (
            f"New order {num} created.\n"
            f"Total: {total}\n"
            f"Customer: {customer_email}\n"
        )

        html = f"""
          <p><strong>New order created.</strong></p>
          {_order_header_html(order)}
          {_line_items_html(order)}
          <p>Customer email: {customer_email}</p>
        """
        return _send_email(subject, _html_to_text(html), html, admin_to)
    except Exception:
        logger.exception(
            "Failed to send admin new order email for order %s",
            getattr(order, "id", None),
        )
        return False

# ------------- Return requests -------------
def send_return_request_emails(return_request):
    order = return_request.order
    customer = order_recipient_email(order)
    en = (
        f"<p>We received your return request for order <b>{order.number}</b>.</p>"
        f"<p>Message:</p><blockquote>{_strip_html(return_request.message)}</blockquote>"
    )
    de = (
        f"<p>Wir haben Ihre Rücksendeanfrage für die Bestellung <b>{order.number}</b> erhalten.</p>"
        f"<p>Nachricht:</p><blockquote>{_strip_html(return_request.message)}</blockquote>"
    )
    if customer:
        _send_bilingual(
            "Return request received / Rücksendeanfrage erhalten",
            customer,
            en,
            de,
        )
    else:
        logger.warning(
            "Return request %s has no customer email; skipping user notification",
            getattr(return_request, "id", None),
        )

    # Admin email (EN only, plain)
    try:
        admin_to = getattr(settings, "ADMIN_EMAIL", None) or DEFAULT_FROM
        if admin_to:
            send_mail(
                subject=f"Return request for order {order.number}",
                message=(
                    f"Order: {order.number}\n"
                    f"Customer: {customer or '-'}\n\n"
                    f"Message:\n{return_request.message}"
                ),
                from_email=DEFAULT_FROM,
                recipient_list=[admin_to],
                fail_silently=not settings.DEBUG,
            )
    except Exception:
        logger.exception("Admin return request email failed")


# ------------- Cancel requests -------------
def send_cancel_request_emails(return_request):
    order = return_request.order
    customer = order_recipient_email(order)
    en = (
        f"<p>We received your cancellation request for order <b>{order.number}</b>.</p>"
        f"<p>Message:</p><blockquote>{_strip_html(return_request.message)}</blockquote>"
    )
    de = (
        f"<p>Wir haben Ihre Stornierungsanfrage für die Bestellung <b>{order.number}</b> erhalten.</p>"
        f"<p>Nachricht:</p><blockquote>{_strip_html(return_request.message)}</blockquote>"
    )
    if customer:
        _send_bilingual(
            "Cancellation request received / Stornierungsanfrage erhalten",
            customer,
            en,
            de,
        )
    else:
        logger.warning(
            "Cancel request %s has no customer email; skipping user notification",
            getattr(return_request, "id", None),
        )

    # Admin email (EN only, plain)
    try:
        admin_to = getattr(settings, "ADMIN_EMAIL", None) or DEFAULT_FROM
        if admin_to:
            send_mail(
                subject=f"Cancellation request for order {order.number}",
                message=(
                    f"Order: {order.number}\n"
                    f"Customer: {customer or '-'}\n\n"
                    f"Message:\n{return_request.message}"
                ),
                from_email=DEFAULT_FROM,
                recipient_list=[admin_to],
                fail_silently=not settings.DEBUG,
            )
    except Exception:
        logger.exception("Admin cancel request email failed")


# ------------- Shipping + cancellation (template-based) -------------
def send_shipping_update(order, tracking: str | None = None):
    to_email = order_recipient_email(order)
    if not to_email:
        return
    ctx = {
        "order": order,
        "tracking": tracking,
        "brand_name": BRAND_NAME,
        "logo_url": _email_logo_url(),
        "site_url": SITE_URL,
    }
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

    en = (
        f"<p>Your order <b>{order.number}</b> has been shipped.</p>"
        f"<p>If available, your tracking number is: {tracking or '-'}.</p>"
    )
    de = (
        f"<p>Ihre Bestellung <b>{order.number}</b> wurde versendet.</p>"
        f"<p>Sofern verfügbar, lautet Ihre Sendungsnummer: {tracking or '-'}.</p>"
    )
    _send_bilingual(
        "Order shipped / Bestellung versendet", to_email, en, de
    )


def send_cancellation_email(order):
    to_email = order_recipient_email(order)
    if not to_email:
        return
    ctx = {
        "order": order,
        "brand_name": BRAND_NAME,
        "logo_url": _email_logo_url(),
        "site_url": SITE_URL,
    }
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
    _send_bilingual(
        "Order cancelled / Bestellung storniert", to_email, en, de
    )


# ------------- Order confirmation -------------
def send_order_confirmation(
    order, user_locale: str = "en", to: Optional[str] = None
) -> bool:
    to = to or order_recipient_email(order)
    if not to:
        return False
    # idempotency: skip if already sent
    if getattr(order, "confirmation_sent_at", None):
        return False

    num = getattr(order, "number", None) or f"#{order.id}"
    subject_en = _tr("en", "subject_order_confirm", num=num)
    subject_de = _tr("de", "subject_order_confirm", num=num)

    body_en = f"""
      <p>{_tr('en','salute')},</p>
      <p>{_tr('en','order_confirm_intro')}</p>
      {_order_header_html(order)}
      {_line_items_html(order)}
      <p>{_tr('en','thanks')}<br />{_tr('en','brand')}</p>
    """
    body_de = f"""
      <p>{_tr('de','salute')},</p>
      <p>{_tr('de','order_confirm_intro')}</p>
      {_order_header_html(order)}
      {_line_items_html(order)}
      <p>{_tr('de','thanks')}<br />{_tr('de','brand')}</p>
    """

    mail = _base_wrapper(subject_en, subject_de, body_en, body_de)
    ok = _send_email(mail.subject, mail.text, mail.html, to)

    if ok:
        type(order).objects.filter(pk=order.pk).update(
            confirmation_sent_at=timezone.now()
        )
        setattr(order, "confirmation_sent_at", timezone.now())

    return ok


# ------------- Installments -------------
def send_installments_started_email(
    order,
    months: Optional[int] = None,
    amounts_eur: Optional[Sequence[Decimal]] = None,
    dates: Optional[timezone.datetime] = None,
    user_locale: str = "en",
    to: Optional[str] = None,
    attachments: Optional[Iterable[Tuple[str, bytes, str]]] = None,
) -> bool:
    """
    Compatible with both signatures:
      - send_installments_started_email(order, to=...)
      - send_installments_started_email(order, months, amounts_eur, dates, to=...)
    If schedule data is missing, send a simple confirmation email.
    """
    to_email = to or order_recipient_email(order)
    if not to_email:
        return False

    num = getattr(order, "number", None) or f"#{order.id}"
    subject_en = _tr("en", "subject_installments_started", num=num)
    subject_de = _tr("de", "subject_installments_started", num=num)

    schedule_html_en = ""
    schedule_html_de = ""

    if months and amounts_eur and dates:
        try:
            rows_en = []
            rows_de = []
            for i, (amt, dt) in enumerate(zip(amounts_eur, dates), start=1):
                when = dt.strftime("%Y-%m-%d")
                amt_disp = _money(amt, CURRENCY)
                rows_en.append(
                    f"<li>Installment {i}: {amt_disp} on {when}</li>"
                )
                rows_de.append(
                    f"<li>Rate {i}: {amt_disp} am {when}</li>"
                )
            if rows_en:
                schedule_html_en = (
                    "<p>Planned schedule:</p><ul>" + "".join(rows_en) + "</ul>"
                )
                schedule_html_de = (
                    "<p>Geplanter Zahlungsplan:</p><ul>"
                    + "".join(rows_de)
                    + "</ul>"
                )
        except Exception:
            logger.exception(
                "Failed to render installments schedule for order %s",
                getattr(order, "id", "?"),
            )

    body_en = f"""
      <p>{_tr('en','salute')},</p>
      <p>Your installment plan for order <b>{num}</b> has started.</p>
      {_order_header_html(order)}
      {_line_items_html(order)}
      {schedule_html_en}
      <p>{_tr('en','thanks')}<br />{_tr('en','brand')}</p>
    """
    body_de = f"""
      <p>{_tr('de','salute')},</p>
      <p>Ihr Ratenplan für die Bestellung <b>{num}</b> wurde gestartet.</p>
      {_order_header_html(order)}
      {_line_items_html(order)}
      {schedule_html_de}
      <p>{_tr('de','thanks')}<br />{_tr('de','brand')}</p>
    """

    mail = _base_wrapper(subject_en, subject_de, body_en, body_de)
    return _send_email(
        mail.subject, mail.text, mail.html, to_email, attachments=attachments
    )


# ------------- Order failed / payment status -------------
def send_order_failed_email(
    order, user_locale: str = "en", to: Optional[str] = None
) -> bool:
    to_email = to or order_recipient_email(order)
    if not to_email:
        return False

    num = getattr(order, "number", None) or f"#{order.id}"
    subject_en = f"Payment for order {num} failed"
    subject_de = f"Zahlung für Bestellung {num} fehlgeschlagen"

    body_en = f"""
      <p>{_tr('en','salute')},</p>
      <p>The payment for your order <b>{num}</b> did not complete.</p>
      {_order_header_html(order)}
      <p>Please try again from your orders page or contact us if you need help.</p>
      <p>{_tr('en','thanks')}<br />{_tr('en','brand')}</p>
    """
    body_de = f"""
      <p>{_tr('de','salute')},</p>
      <p>Die Zahlung für Ihre Bestellung <b>{num}</b> wurde nicht abgeschlossen.</p>
      {_order_header_html(order)}
      <p>Bitte versuchen Sie es erneut über Ihre Bestellseite oder kontaktieren Sie uns, wenn Sie Hilfe benötigen.</p>
      <p>{_tr('de','thanks')}<br />{_tr('de','brand')}</p>
    """

    mail = _base_wrapper(subject_en, subject_de, body_en, body_de)
    return _send_email(mail.subject, mail.text, mail.html, to_email)


def send_payment_status_email(
    order,
    status_en: str,
    status_de: str,
    extra_en: str = "",
    extra_de: str = "",
    user_locale: str = "en",
    to: Optional[str] = None,
) -> bool:
    """
    Generic payment status email helper for edge cases.
    """
    to_email = to or order_recipient_email(order)
    if not to_email:
        return False

    num = getattr(order, "number", None) or f"#{order.id}"
    subject_en = _tr("en", "subject_payment_status", num=num)
    subject_de = _tr("de", "subject_payment_status", num=num)

    body_en = f"""
      <p>{_tr('en','salute')},</p>
      <p>{_tr('en','payment_status_intro')}</p>
      {_order_header_html(order)}
      <p><strong>Status:</strong> {status_en}</p>
      {extra_en}
      <p>{_tr('en','thanks')}<br />{_tr('en','brand')}</p>
    """
    body_de = f"""
      <p>{_tr('de','salute')},</p>
      <p>{_tr('de','payment_status_intro')}</p>
      {_order_header_html(order)}
      <p><strong>Status:</strong> {status_de}</p>
      {extra_de}
      <p>{_tr('de','thanks')}<br />{_tr('de','brand')}</p>
    """

    mail = _base_wrapper(subject_en, subject_de, body_en, body_de)
    return _send_email(mail.subject, mail.text, mail.html, to_email)


# ------------- Invoice email -------------
def _order_email(order):
    return order_recipient_email(order)


def send_invoice_email(
    order, user_locale: str = "en", to: Optional[str] = None
):
    to_email = to or _order_email(order)
    if not to_email:
        return False

    # ✅ Ensure invoice exists (idempotent – safe to call multiple times)
    try:
        generate_invoice_pdf(order)
    except Exception:
        logger.exception(
            "send_invoice_email: failed to (re)generate invoice for order %s",
            getattr(order, "id", "?"),
        )

    num = getattr(order, "number", None) or f"#{order.id}"
    subject_en = _tr("en", "subject_invoice", num=num)
    subject_de = _tr("de", "subject_invoice", num=num)

    # Try guest invoice download link (for guest orders)
    guest_link = None
    try:
        guest_link = build_guest_invoice_download_url(order)
    except Exception:
        logger.exception(
            "Failed to build guest invoice download URL for order %s",
            getattr(order, "id", "?"),
        )

    guest_html_en = (
        f"<p>You can also download your invoice here: "
        f'<a href="{guest_link}">{guest_link}</a></p>'
        if guest_link
        else ""
    )
    guest_html_de = (
        f"<p>Sie können Ihre Rechnung auch hier herunterladen: "
        f'<a href="{guest_link}">{guest_link}</a></p>'
        if guest_link
        else ""
    )

    body_en = f"""
      <p>{_tr('en','salute')},</p>
      <p>Attached you will find your invoice for order <b>{num}</b>.</p>
      {_order_header_html(order)}
      {_line_items_html(order)}
      {guest_html_en}
      <p>{_tr('en','thanks')}<br />{_tr('en','brand')}</p>
    """
    body_de = f"""
      <p>{_tr('de','salute')},</p>
      <p>Im Anhang finden Sie Ihre Rechnung für die Bestellung <b>{num}</b>.</p>
      {_order_header_html(order)}
      {_line_items_html(order)}
      {guest_html_de}
      <p>{_tr('de','thanks')}<br />{_tr('de','brand')}</p>
    """

    mail = _base_wrapper(subject_en, subject_de, body_en, body_de)

    try:
        with get_connection() as conn:
            msg = EmailMultiAlternatives(
                subject=mail.subject,
                body=mail.text,
                from_email=_from_email(),
                to=[to_email],
                bcc=list(_bcc_defaults()),
                connection=conn,
            )
            msg.attach_alternative(mail.html, "text/html")
            _attach_invoice_if_available(order, msg)
            msg.send()

        now = timezone.now()
        type(order).objects.filter(pk=order.pk).update(
            invoice_sent_at=now
        )
        setattr(order, "invoice_sent_at", now)

        # ✅ After customer invoice email is sent, notify admin (best-effort)
        try:
            send_admin_order_paid_email(order)
        except Exception:
            logger.exception(
                "Failed to send admin 'order paid' email for order %s",
                getattr(order, "id", "?"),
            )

        return True
    except Exception:
        logger.exception(
            "Failed to send invoice email for order %s",
            getattr(order, "id", "?"),
        )
        return False


# ------------- Stock alert (wishlist back-in-stock) -------------
def send_stock_alert_email(
    product, to_email: str, unsubscribe_url: str, user_locale: str = "en"
):
    subject_en = f"Back in stock: {getattr(product, 'title_en', str(product))}"
    subject_de = (
        f"Wieder verfügbar: "
        f"{getattr(product, 'title_de', getattr(product, 'title_en', str(product)))}"
    )
    subj = f"{subject_en} / {subject_de}"

    ctx = {
        "product": product,
        "product_url": f"{SITE_URL}/product/{product.id}",
        "unsubscribe_url": unsubscribe_url,
        "brand_name": BRAND_NAME,
        "logo_url": _email_logo_url(),
        "site_url": SITE_URL,
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
