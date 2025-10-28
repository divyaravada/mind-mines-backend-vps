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

logger = logging.getLogger(__name__)
CURRENCY = "€"

ADMIN_EMAIL = getattr(settings, "SUPPORT_EMAIL", "info@mindmines.eu")
DEFAULT_FROM = getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@example.com")
ORDERS_ADMIN_EMAIL = getattr(settings, "ORDERS_ADMIN_EMAIL", "info@mindmines.eu")
SITE_URL = getattr(settings, "FRONTEND_URL", "https://mindmines.eu").rstrip("/")
FRONTEND_ORDERS_URL = f"{SITE_URL}/account/orders"

MAX_ATTACH_BYTES = 14 * 1024 * 1024

def order_recipient_email(order) -> Optional[str]:
    """
    Recipient for order emails in authenticated-only flow.
    Priority: explicit order.email, then order.user.email.
    """
    return (getattr(order, "email", None)
            or getattr(getattr(order, "user", None), "email", None))


def _money(v: Decimal, currency: str = "EUR") -> str:
    try:
        return f"{Decimal(v):.2f} {currency}"
    except Exception:
        return f"{v} {currency}"


def _from_email() -> str:
    return getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@mindmines.eu")


def _bcc_defaults() -> Sequence[str]:
    """
    Optional: set in settings.py
      ORDER_EMAIL_BCC = ["orders@mindmines.eu"]
    """
    return getattr(settings, "ORDER_EMAIL_BCC", [])


def _attach_invoice_if_available(order, email_msg: EmailMultiAlternatives):
    """
    Attach invoice PDF if present (either through related Invoice.pdf
    or legacy Order.invoice_pdf).
    """
    try:
        inv = getattr(order, "invoice", None)
        if inv and getattr(inv, "pdf", None) and inv.pdf:
            name = f"{getattr(inv, 'number', f'invoice_{order.id}')}.pdf"
            email_msg.attach(name, inv.pdf.read(), "application/pdf")
            return
    except Exception:
        logger.exception("Failed reading related invoice pdf for order %s", order.id)

    try:
        f = getattr(order, "invoice_pdf", None)
        if f:
            email_msg.attach(f.name.split("/")[-1], f.read(), "application/pdf")
    except Exception:
        logger.exception("Failed reading legacy invoice pdf for order %s", order.id)


@dataclass
class EmailBody:
    subject: str
    text: str
    html: str


def _tr(locale: str, key: str, **vars) -> str:
    """
    Tiny inline i18n. locale in {'en','de'}.
    """
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
        "subject_order_confirm": {
            "en": "Order {num} confirmed — thank you!",
            "de": "Bestellung {num} bestätigt — vielen Dank!",
        },
        "subject_bank_transfer": {
            "en": "Bank transfer instructions for Order {num}",
            "de": "Überweisungsanleitung für Bestellung {num}",
        },
        "subject_installments_started": {
            "en": "Installment plan started for Order {num}",
            "de": "Ratenplan gestartet für Bestellung {num}",
        },
        "salute": {
            "en": "Hello",
            "de": "Hallo",
        },
        "thanks": {
            "en": "Thank you for shopping with MindMines.",
            "de": "Vielen Dank für Ihren Einkauf bei MindMines.",
        },
        "brand": {
            "en": "MindMines",
            "de": "MindMines",
        },
        "order_label": {
            "en": "Order",
            "de": "Bestellung",
        },
        "total_label": {
            "en": "Total",
            "de": "Gesamtsumme",
        },
        "sepa_pending_intro": {
            "en": "We received your SEPA Direct Debit authorization. Your payment is being processed (can take 1–3 business days).",
            "de": "Wir haben Ihre SEPA-Lastschriftmandat erhalten. Ihre Zahlung wird verarbeitet (dies kann 1–3 Werktage dauern).",
        },
        "we_will_email_updates": {
            "en": "We’ll email you once the payment clears and your invoice is available.",
            "de": "Wir informieren Sie per E-Mail, sobald die Zahlung eingegangen ist und Ihre Rechnung verfügbar ist.",
        },
        "order_confirm_intro": {
            "en": "We’ve confirmed your order and will notify you as it moves through processing and shipment.",
            "de": "Wir haben Ihre Bestellung bestätigt und informieren Sie über den weiteren Verlauf bis zum Versand.",
        },
        "invoice_ready_intro": {
            "en": "Your payment has been received. Your invoice is attached to this email.",
            "de": "Ihre Zahlung ist eingegangen. Die Rechnung ist dieser E-Mail beigefügt.",
        },
        "bank_transfer_intro": {
            "en": "Please transfer the total amount using the details below.",
            "de": "Bitte überweisen Sie den Gesamtbetrag mit den folgenden Angaben.",
        },
        "pay_within_hours": {
            "en": "Please complete the transfer within {hrs} hours.",
            "de": "Bitte schließen Sie die Überweisung innerhalb von {hrs} Stunden ab.",
        },
        "reference_note": {
            "en": "Use the reference exactly as shown so we can match your payment.",
            "de": "Bitte verwenden Sie den Verwendungszweck genau wie angegeben, damit wir Ihre Zahlung zuordnen können.",
        },
        "installments_started_intro": {
            "en": "Your installment plan has been set up. Below is the expected schedule and amounts.",
            "de": "Ihr Ratenplan wurde eingerichtet. Unten sehen Sie die erwarteten Termine und Beträge.",
        },
        "kind_regards": {
            "en": "Kind regards",
            "de": "Mit freundlichen Grüßen",
        },
    }
    s = t[key]["en" if loc not in ("en", "de") else loc]
    try:
        return s.format(**vars)
    except Exception:
        return s


def _bilingual_block(en: str, de: str) -> Tuple[str, str]:
    """Return text_en, text_de with a separator for plaintext."""
    sep = "\n\n" + ("-" * 32) + "\n\n"
    return en, de + ""  # no change; we assemble later


def _base_wrapper(subject_en: str, subject_de: str, body_en_html: str, body_de_html: str) -> EmailBody:
    subject = f"{subject_en} / {subject_de}"

    # Plain text fallback
    text_en = _html_to_text(body_en_html)
    text_de = _html_to_text(body_de_html)
    text = f"{text_en}\n\n{'-'*40}\n\n{text_de}"

    html = f"""
      <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;line-height:1.5;color:#111">
        <div>
          <div>{body_en_html}</div>
          <hr style="margin:24px 0;border:none;border-top:1px solid #eee" />
          <div>{body_de_html}</div>
        </div>
        <div style="margin-top:24px;color:#666;font-size:12px">
          {_tr('en','brand')} • www.mindmines.eu
        </div>
      </div>
    """.strip()
    return EmailBody(subject=subject, text=text, html=html)


def _html_to_text(html: str) -> str:
    # Keep it simple; Django's linebreaksbr for safety, then strip tags crudely
    try:
        s = html.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
        for tag in ("p", "div", "ul", "ol", "li", "strong", "em", "b", "i", "hr"):
            s = s.replace(f"<{tag}>", "").replace(f"</{tag}>", "")
        return s
    except Exception:
        return html


def _send_email(subject: str, text: str, html: str, to_email: str, bcc: Optional[Iterable[str]] = None,
                attachments: Optional[Iterable[Tuple[str, bytes, str]]] = None) -> bool:
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
        logger.exception("Failed to send email to %s (subject=%s)", to_email, subject)
        return False


# ---------------------------------------------------------------------------
# Email builders
# ---------------------------------------------------------------------------

def _order_header_html(order) -> str:
    num = getattr(order, "number", None) or f"#{order.id}"
    total = _money(getattr(order, "total_price", Decimal("0.00")))
    return f"""
      <p><strong>{_tr('en','order_label')}: {num}</strong><br />
         {_tr('en','total_label')}: {total}</p>
    """


def _line_items_html(order) -> str:
    try:
        rows = []
        items = getattr(order, "items", None)
        if items is None:
            return ""
        for it in items.all().select_related("product"):
            title = getattr(getattr(it, "product", None), "title_en", None) or \
                    getattr(getattr(it, "product", None), "title", "") or "Item"
            qty = getattr(it, "quantity", 1)
            unit = _money(getattr(it, "unit_price", Decimal("0.00")))
            rows.append(f"<li>{title} &times; {qty} — {unit}</li>")
        if rows:
            return "<ul>" + "".join(rows) + "</ul>"
    except Exception:
        logger.exception("Failed to render line items for order %s", order.id)
    return ""


def _fmt(amount): return f"{float(amount):.2f} {CURRENCY}"

def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()

def get_order_recipient(order) -> str | None:
    """
    Authenticated-only priority: explicit order.email -> order.user.email.
    """
    for cand in [
        getattr(order, "email", None),
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
def send_sepa_pending_email(order, user_locale: str = "en", to: Optional[str] = None) -> bool:
    to_email = to or order_recipient_email(order)
    num = getattr(order, "number", None) or f"#{order.id}"

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
        logger.exception("Admin return request email failed")

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
        logger.exception("Admin cancellation request email failed")

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
def send_order_confirmation(order, user_locale: str = "en", to: Optional[str] = None) -> bool:
    to_email = to or order_recipient_email(order)
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
    return _send_email(mail.subject, mail.text, mail.html, to_email)


# ------------- Bank transfer -------------
def send_bank_transfer_instructions(
    order,
    bank_details: dict,
    user_locale: str = "en",
    due_hours: int = 48,
    to: Optional[str] = None,
) -> bool:
    """
    bank_details = {"iban": "...", "bic": "...", "account_name": "..."}
    """
    to_email = to or order_recipient_email(order)
    num = getattr(order, "number", None) or f"#{order.id}"
    amt = _money(getattr(order, "total_price", Decimal("0.00")))
    iban = (bank_details or {}).get("iban", "")
    bic = (bank_details or {}).get("bic", "")
    acc = (bank_details or {}).get("account_name", "")
    ref = getattr(order, "number", f"ORD-{order.id}")

    subject_en = _tr("en", "subject_bank_transfer", num=num)
    subject_de = _tr("de", "subject_bank_transfer", num=num)

    body_en = f"""
      <p>{_tr('en','salute')},</p>
      <p>{_tr('en','bank_transfer_intro')}</p>
      {_order_header_html(order)}
      <p><strong>Amount:</strong> {amt}<br>
         <strong>IBAN:</strong> {iban}<br>
         <strong>BIC:</strong> {bic}<br>
         <strong>Beneficiary:</strong> {acc}<br>
         <strong>Reference:</strong> {ref}</p>
      <p>{_tr('en','pay_within_hours', hrs=due_hours)}</p>
      <p><em>{_tr('en','reference_note')}</em></p>
      <p>{_tr('en','kind_regards')}<br />{_tr('en','brand')}</p>
    """

    body_de = f"""
      <p>{_tr('de','salute')},</p>
      <p>{_tr('de','bank_transfer_intro')}</p>
      {_order_header_html(order)}
      <p><strong>Betrag:</strong> {amt}<br>
         <strong>IBAN:</strong> {iban}<br>
         <strong>BIC:</strong> {bic}<br>
         <strong>Empfänger:</strong> {acc}<br>
         <strong>Verwendungszweck:</strong> {ref}</p>
      <p>{_tr('de','pay_within_hours', hrs=due_hours)}</p>
      <p><em>{_tr('de','reference_note')}</em></p>
      <p>{_tr('de','kind_regards')}<br />{_tr('de','brand')}</p>
    """

    mail = _base_wrapper(subject_en, subject_de, body_en, body_de)
    return _send_email(mail.subject, mail.text, mail.html, to_email)



# ------------- Installments -------------
def send_installments_started_email(
    order,
    months: int,
    amounts_eur: Sequence[Decimal],
    dates: Sequence[timezone.datetime],
    user_locale: str = "en",
    to: Optional[str] = None,
) -> bool:
    to_email = to or order_recipient_email(order)
    num = getattr(order, "number", None) or f"#{order.id}"

    subject_en = _tr("en", "subject_installments_started", num=num)
    subject_de = _tr("de", "subject_installments_started", num=num)

    # Build schedule table
    try:
        rows_en = []
        rows_de = []
        for amt, dt in zip(amounts_eur, dates):
            when = timezone.localtime(dt).strftime("%Y-%m-%d")
            rows_en.append(f"<tr><td>{when}</td><td>{_money(amt)}</td></tr>")
            rows_de.append(f"<tr><td>{when}</td><td>{_money(amt)}</td></tr>")
        table_en = "<table cellpadding='6' cellspacing='0' border='1'><tr><th>Date</th><th>Amount</th></tr>" + "".join(rows_en) + "</table>"
        table_de = "<table cellpadding='6' cellspacing='0' border='1'><tr><th>Datum</th><th>Betrag</th></tr>" + "".join(rows_de) + "</table>"
    except Exception:
        logger.exception("Failed to render installments schedule for order %s", order.id)
        table_en = table_de = ""

    body_en = f"""
      <p>{_tr('en','salute')},</p>
      <p>{_tr('en','installments_started_intro')}</p>
      {_order_header_html(order)}
      {table_en}
      <p>{_tr('en','thanks')}<br />{_tr('en','brand')}</p>
    """
    body_de = f"""
      <p>{_tr('de','salute')},</p>
      <p>{_tr('de','installments_started_intro')}</p>
      {_order_header_html(order)}
      {table_de}
      <p>{_tr('de','thanks')}<br />{_tr('de','brand')}</p>
    """

    mail = _base_wrapper(subject_en, subject_de, body_en, body_de)
    return _send_email(mail.subject, mail.text, mail.html, to_email)

# ------------- Invoice email -------------
def send_invoice_email(order, user_locale: str = "en", to: Optional[str] = None) -> bool:
    to_email = to or order_recipient_email(order)
    num = getattr(order, "number", None) or f"#{order.id}"

    subject_en = _tr("en", "subject_invoice", num=num)
    subject_de = _tr("de", "subject_invoice", num=num)

    body_en = f"""
      <p>{_tr('en','salute')},</p>
      <p>{_tr('en','invoice_ready_intro')}</p>
      {_order_header_html(order)}
      {_line_items_html(order)}
      <p>{_tr('en','thanks')}<br />{_tr('en','brand')}</p>
    """

    body_de = f"""
      <p>{_tr('de','salute')},</p>
      <p>{_tr('de','invoice_ready_intro')}</p>
      {_order_header_html(order)}
      {_line_items_html(order)}
      <p>{_tr('de','thanks')}<br />{_tr('de','brand')}</p>
    """

    mail = _base_wrapper(subject_en, subject_de, body_en, body_de)

    # Build and send with invoice attachment
    try:
        with get_connection() as conn:
            msg = EmailMultiAlternatives(
                subject=mail.subject,
                body=mail.text,
                from_email=_from_email(),
                to=[to_email] if to_email else [],
                bcc=list(_bcc_defaults()),
                connection=conn,
            )
            msg.attach_alternative(mail.html, "text/html")
            _attach_invoice_if_available(order, msg)
            if not msg.to:
                logger.warning("No recipient for invoice email on order %s", order.id)
                return False
            msg.send()
        return True
    except Exception:
        logger.exception("Failed to send invoice email for order %s", getattr(order, "id", "?"))
        return False


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