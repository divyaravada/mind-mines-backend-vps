
import os
import logging
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.timezone import localtime, now as tz_now
from django.utils.translation import activate, get_language
from django.core.mail import send_mail,EmailMultiAlternatives
from django.contrib.staticfiles import finders
from email.mime.image import MIMEImage
import mimetypes
from decimal import Decimal
from server.models import ReturnRequest  # Adjust the import path as needed
from django.template import TemplateDoesNotExist

log = logging.getLogger(__name__)
CURRENCY = "€"

ADMIN_EMAIL = getattr(settings, "SUPPORT_EMAIL", "info@mindmines.eu")
DEFAULT_FROM = getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@example.com")
DEFAULT_ADMIN_EMAIL = getattr(settings, "ORDERS_NOTIF_EMAIL", "info@mindmines.eu")
SITE_NAME = getattr(settings, "SITE_NAME", "MindMines")
ORDERS_ADMIN_EMAIL = getattr(settings, "ORDERS_ADMIN_EMAIL", "info@mindmines.eu")

# Attach up to ~10 MB per file (adjust as needed)
MAX_ATTACH_BYTES = 14 * 1024 * 1024



def _fmt(amount): return f"{float(amount):.2f} {CURRENCY}"

FRONTEND_ORDERS_URL = getattr(settings, "FRONTEND_ORDERS_URL", "http://localhost:3000/account/orders")





def _payment_method_label(order) -> str:
    """
    Prefer Django's built-in display from model choices; fall back to mapping.
    """
    try:
        # Works if payment_method has choices on the model
        disp = order.get_payment_method_display()
        if disp:
            return str(disp)
    except Exception:
        pass
    # fallback mapping
    mapping = {
        "sepa_debit": "SEPA Direct Debit",
        "card": "Card",
        "paypal": "PayPal",
        "klarna": "Klarna",
        "bank": "Bank Transfer",
    }
    code = (getattr(order, "payment_method", "") or "").lower()
    return mapping.get(code, code.title() or "Payment")

def send_sepa_pending_email(order, user_locale="en"):
    """
    Immediate email telling the customer SEPA can take 2–5 banking days.
    No invoice is attached here.
    Idempotent: safe to call multiple times; the user just sees the same info.
    """
    prior = get_language()
    try:
        activate(user_locale or "en")

        to_email = getattr(order.user, "email", None)
        if not to_email:
            return

        created_display = localtime(order.created_at).strftime("%Y-%m-%d %H:%M")

        # Build a minimal context
        ctx = {
            "order": order,
            "created_at_display": created_display,
            "payment_method_display": _payment_method_label(order),
            "total_display": _fmt(order.total_price or 0),
            "sepa_notice_days": "2–5",
            "orders_url": getattr(settings, "FRONTEND_ORDERS_URL", "http://localhost:3000/account/orders"),
        }

        # Try templates first; if missing, use inline fallbacks
        try:
            subject = render_to_string("emails/sepa_pending_subject.txt", ctx).strip()
        except Exception:
            subject = f"SEPA payment initiated for order {order.number}"

        try:
            text = render_to_string("emails/sepa_pending.txt", ctx)
        except Exception:
            text = (
                f"Thanks! We’ve received your SEPA Direct Debit request for order {order.number}.\n\n"
                f"The bank will process the payment in 2–5 working days. "
                f"We’ll email your invoice once the payment succeeds.\n\n"
                f"Order total: {ctx['total_display']}\n"
                f"Placed at: {ctx['created_at_display']}\n\n"
                f"You can review your order here: {ctx['orders_url']}\n"
            )

        try:
            html = render_to_string("emails/sepa_pending.html", ctx)
        except Exception:
            html = (
                f"<p>Thanks! We’ve received your <b>SEPA Direct Debit</b> request for order <b>{order.number}</b>.</p>"
                f"<p>The bank will process the payment in <b>2–5 working days</b>. "
                f"We’ll email your <b>invoice</b> once the payment succeeds.</p>"
                f"<p><b>Order total:</b> {ctx['total_display']}<br>"
                f"<b>Placed at:</b> {ctx['created_at_display']}</p>"
                f"<p><a href='{ctx['orders_url']}'>View your orders</a></p>"
            )

        msg = EmailMultiAlternatives(
            subject=subject or f"SEPA payment initiated for {order.number}",
            body=text,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[to_email],
            reply_to=getattr(settings, "ORDER_EMAIL_REPLY_TO", []),
        )
        msg.attach_alternative(html, "text/html")
        msg.send(fail_silently=True)

    finally:
        activate(prior)


def _build_items_table_and_totals(order):
    rows = []
    subtotal = Decimal("0.00")

    for oi in order.items.select_related("product"):
        name = getattr(oi, "product_name", None) or getattr(oi.product, "title_en", None) or str(oi.product)
        qty = int(oi.quantity)
        unit = Decimal(oi.unit_price)
        line_total = oi.line_total()  # returns Decimal with 2dp

        subtotal += line_total
        rows.append({
            "name": name,
            "qty": qty,
            "unit_display": _fmt(unit),
            "total_display": _fmt(line_total),
        })

    shipping = Decimal(getattr(order, "shipping_fee", 0) or 0)
    total = (subtotal + shipping).quantize(Decimal("0.01"))

    text_lines = [f"- {r['name']} x{r['qty']} — {r['total_display']}" for r in rows]

    return rows, text_lines, subtotal, shipping, total

def _file_mimetype(name: str) -> str:
    guess, _ = mimetypes.guess_type(name or "")
    return guess or "application/octet-stream"

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
        cid = "brandlogo"  # any stable id
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename="logo.png")
        email_msg.attach(img)
        return cid
    except Exception:
        return None

def _items_for_email(order):
    items = []
    for line in order.items.all():
        name = getattr(line, "product_name", None) or getattr(getattr(line, "product", None), "title_en", None) or str(line.product)
        qty = int(line.quantity or 0)
        total = (float(line.unit_price or 0) * qty) if line.unit_price is not None else 0.0
        items.append({
            "name": name,
            "qty": qty,
            "unit": _fmt(line.unit_price or 0),
            "total": _fmt(total),
        })
    return items

def _render_or_fallback(txt_name, html_name, ctx, txt_fb, html_fb):
    try:
        txt = render_to_string(txt_name, ctx)
    except TemplateDoesNotExist:
        txt = txt_fb.format(**ctx)
    try:
        html = render_to_string(html_name, ctx)
    except TemplateDoesNotExist:
        html = html_fb.format(**ctx)
    return txt, html

def _customer_name_from_order(order) -> str:
    addr = getattr(order, "shipping_address", None)
    if addr:
        full = (getattr(addr, "full_name", "") or "").strip()
        if full:
            return full
        first = (getattr(addr, "first_name", "") or "").strip()
        last  = (getattr(addr, "last_name", "") or "").strip()
        combo = " ".join(x for x in (first, last) if x)
        if combo:
            return combo
    user = getattr(order, "user", None)
    if user:
        try:
            full = (user.get_full_name() or "").strip()
        except Exception:
            full = ""
        if full:
            return full
        first = (getattr(user, "first_name", "") or "").strip()
        last  = (getattr(user, "last_name", "") or "").strip()
        combo = " ".join(x for x in (first, last) if x)
        if combo:
            return combo
        uname = (getattr(user, "username", "") or "").strip()
        if uname:
            return uname
        email = (getattr(user, "email", "") or "").strip()
        if email:
            return email.split("@")[0]
    return "Customer"

def _abs_media_url(file_field, request=None):
    """
    Build an absolute URL for the file so the admin can click it from email.
    Works with:
      - request.build_absolute_uri()
      - settings.MEDIA_BASE_URL / SITE_BASE_URL as a fallback
    """
    try:
        url = file_field.url
    except Exception:
        return None

    # If already absolute, return as-is
    if url.startswith("http://") or url.startswith("https://"):
        return url

    # Prefer request host if available
    if request is not None:
        return request.build_absolute_uri(url)

    # Otherwise use configured base
    base = (
        getattr(settings, "MEDIA_BASE_URL", None)
        or getattr(settings, "SITE_BASE_URL", None)
        or getattr(settings, "FRONTEND_ORIGIN", None)
    )
    if base:
        return f"{base.rstrip('/')}{url}"
    return None


def _file_meta(att, request=None):
    """
    Return (basename, url, size_bytes, mimetype) for an attachment row.
    """
    f = att.file
    name = getattr(f, "name", "attachment")
    base = name.rsplit("/", 1)[-1]
    url  = _abs_media_url(f, request=request)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    size = getattr(f, "size", None)
    return base, url, size, mime


def send_cancel_request_emails(rr):
    """
    Send two emails:
      - to the customer: “we received your cancellation request”
      - to support/admin: details of the request
    """
    order = rr.order
    user = rr.user

    # Compute simple, ready-to-print values
    placed_display = localtime(order.created_at).strftime("%Y-%m-%d %H:%M")
    total_display  = _fmt(order.total_price or 0)
    subtotal_display = _fmt(getattr(order, "subtotal", 0) or 0)
    shipping_display = _fmt(getattr(order, "shipping_fee", 0) or 0)

    # Safe customer name/email
    try:
        name = user.get_full_name() or ""
    except Exception:
        name = ""
    if not name:
        name = getattr(user, "username", "") or getattr(user, "email", "")
    email = getattr(user, "email", "")



    user_email = getattr(user, "email", None)
    support = getattr(settings, "SUPPORT_EMAIL", None) or getattr(settings, "DEFAULT_FROM_EMAIL", None)

    # Build a common context
    ctx = {
        "now": tz_now(),                       # if you want to keep using it
        "year": timezone.now().year, 
        "order": order,
        "rr": rr,
        "orders_url": FRONTEND_ORDERS_URL,
        "created_at_display": localtime(order.created_at).strftime("%Y-%m-%d %H:%M"),
        "placed_display": placed_display,
        "items": _items_for_email(order),
        "subtotal_display": _fmt(order.subtotal or 0),
        "shipping_display": _fmt(getattr(order, "shipping_fee", 0) or 0),
        "total_display": _fmt(order.total_price or 0),
        "message": (rr.message or "").strip(),
        "customer_name": name,
        "customer_email": email,
    }
  # ---- Customer email ----
    if email:
        subject = f"We received your cancellation request for order {order.number}"
        text = render_to_string("emails/cancel_request_user.txt", ctx)
        msg = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, [user_email])

        cid = _attach_inline_logo(msg)
        if cid: ctx["logo_cid"] = cid
        html = render_to_string("emails/cancel_request_user.html", ctx)
        msg.attach_alternative(html, "text/html")
        msg.send(fail_silently=False)

    # ---- Admin/support email ----
    support = getattr(settings, "SUPPORT_EMAIL", None) or getattr(settings, "DEFAULT_FROM_EMAIL", None)
    if support:
        subject = f"[Admin] Cancellation request #{rr.pk} — order {order.number}"
        text = render_to_string("emails/cancel_request_admin.txt", ctx)
        to = [support]
        # also include ADMINS if configured
        admins = [e for _, e in getattr(settings, "ADMINS", [])]
        if admins: to.extend(admins)

        msg2 = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, to)
        cid2 = _attach_inline_logo(msg2)
        if cid2: ctx["logo_cid"] = cid2
        html2 = render_to_string("emails/cancel_request_admin.html", ctx)
        msg2.attach_alternative(html2, "text/html")
        msg2.send(fail_silently=False)

def send_return_request_emails( rr, request=None):
    """
    Sends:
      - Customer confirmation (without heavy attachments)
      - Admin notification (with attachments)
    Raises on failure so callers can act accordingly.
    """
    order = rr.order
    user = rr.user
    user_to = [user.email] if getattr(user, "email", None) else []
    admin_to = [ADMIN_EMAIL]
    user = rr.user
    customer_name = _customer_name_from_order(order)

    # Build links + small-image attachments
    files_ctx = []
    admin_attachments = []

    for att in rr.attachments.all():
        base, url, size, mime = _file_meta(att, request=request)
        # add to template context
        files_ctx.append({
            "name": base,
            "url": url,
            "size_kb": round((size or 0) / 1024, 1) if size is not None else None,
            "mime": mime,
            "attached": bool(size is not None and size <= MAX_ATTACH_BYTES),
        })
        # try attaching small files for admin
        if size is not None and size <= MAX_ATTACH_BYTES:
            try:
                with att.file.open("rb") as fp:
                    admin_attachments.append((base, fp.read(), mime))
            except Exception:
                # ignore read errors; admin still gets links in the body
                pass

    ctx = {
        "order": order,
        "rr": rr,
        "type_display": "Return",
        "created_at_display": localtime(rr.created_at).strftime("%Y-%m-%d %H:%M"),
        "order_created_at_display": localtime(order.created_at).strftime("%Y-%m-%d %H:%M"),
        "customer_name": customer_name,
        "customer_email": getattr(user, "email", ""),
        "message": rr.message or "",
        "orders_url": FRONTEND_ORDERS_URL,
        "files": files_ctx,
        "year": tz_now().year,
    }

    # --- Customer email (no attachments) ---
    to_user = [user.email] if getattr(user, "email", None) else []
    if to_user:
        subject_u = f"We received your return request for order {order.number}"
        text_u = render_to_string("emails/return_user.txt", ctx)
        msg_u  = EmailMultiAlternatives(subject_u, text_u, DEFAULT_FROM, to_user)
        cid = _attach_inline_logo(msg_u)
        if cid:
            ctx["logo_cid"] = cid
        html_u = render_to_string("emails/return_user.html", ctx)
        msg_u.attach_alternative(html_u, "text/html")
        msg_u.send(fail_silently=False)

    # --- Admin email (attach small files + include links for all) ---
    subject_a = f"[Return] {order.number} • {ctx['created_at_display']} • {customer_name}"
    text_a = render_to_string("emails/return_admin.txt", ctx)
    msg_a  = EmailMultiAlternatives(subject_a, text_a, DEFAULT_FROM, [ADMIN_EMAIL])
    cid_a  = _attach_inline_logo(msg_a)
    if cid_a:
        ctx["logo_cid"] = cid_a
    html_a = render_to_string("emails/return_admin.html", ctx)
    msg_a.attach_alternative(html_a, "text/html")
    for fname, content, mime in admin_attachments:
        msg_a.attach(fname, content, mime)
    msg_a.send(fail_silently=False)

def send_shipping_update(order):
    subject = f"Your order {order.number} has shipped"
    body = (
        f"Good news!\n\nOrder {order.number} has shipped."
        + (f"\nTracking number: {order.tracking_number}\n" if order.tracking_number else "\n")
    )
    send_mail(subject, body, getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@example.com"), [order.user.email])

def send_cancellation_email(order):
    subject = f"Your order {order.number} has been cancelled"
    body = f"Hi,\n\nYour order {order.number} was cancelled. If this is unexpected, reply to this email."
    send_mail(subject, body, getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@example.com"), [order.user.email])

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

def send_order_confirmation(order, user_locale="en"):
    prior_lang = get_language()
    try:
        activate(user_locale or "en")
        ctx = build_order_context(order)
        subject = render_to_string("emails/order_subject.txt", {"order": order}).strip() if False else f"Order {order.number} confirmed"
        text = render_to_string("emails/order_confirmation.txt", ctx)
        html = render_to_string("emails/order_confirmation.html", ctx)

        to_email = getattr(order, "email", None) or getattr(order.user, "email", None)
        if not to_email:
            return  # nothing to send to

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[to_email],
            reply_to=getattr(settings, "ORDER_EMAIL_REPLY_TO", []),
        )
        msg.attach_alternative(html, "text/html")
        attach_invoice_if_available(msg, order)
        msg.send(fail_silently=True)
    finally:
        activate(prior_lang)


# ------------------ BANK TRANSFER ------------------
def send_bank_transfer_instructions(order, bank, user_locale="en", due_hours=None):
    prior_lang = get_language()
    try:
        activate(user_locale or "en")

        # Build items summary
        rows, text_lines, subtotal, shipping, total = _build_items_table_and_totals(order)

        ctx = {
            "order": order,
            "bank": bank,
            "items_table": rows,                     # [{name, qty, unit_display, total_display}]
            "items_text": "\n".join(text_lines),     # plain text version
            "subtotal_display": _fmt(subtotal),
            "shipping_display": "Free" if shipping == 0 else _fmt(shipping),
            "total_display": _fmt(total),

            # keep these for other parts of the email if you like:
            "amount_display": _fmt(total),           # legacy alias; equals total_display
             # NEW / IMPORTANT
            "payment_method_display": _payment_method_label(order),
            "created_at_display": localtime(order.created_at).strftime("%Y-%m-%d %H:%M"),
            "orders_url": FRONTEND_ORDERS_URL,
            "now": timezone.now(),

            # keep these if you still reference them anywhere
            "reference": order.number,
            "beneficiary": bank["account_name"],
            "iban": bank["iban"],
            "bic": bank["bic"],
            "due_hours": due_hours, 
        }

        subject = f"Bank transfer instructions for order {order.number} / Überweisungsdetails zu {order.number}"
        to = [order.user.email]

        # Text body
        text = render_to_string("emails/bank_transfer.txt", ctx)
        msg = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, to)

        # Inline logo → set logo_cid BEFORE rendering HTML
        cid = _attach_inline_logo(msg)
        if cid:
            ctx["logo_cid"] = cid

        html = render_to_string("emails/bank_transfer.html", ctx)
        msg.attach_alternative(html, "text/html")
        log.info("EMAIL: sending bank transfer to %s (order %s)", to, order.id)
        msg.send(fail_silently=False)
        log.info("EMAIL: sent bank transfer to %s OK", to)
    finally:
        activate(prior_lang)


# ------------------ INVOICE (PAID) ------------------
def send_invoice_email(order, user_locale="en"):
    to_email = getattr(order.user, "email", None)
    if not to_email:
        log.warning("Invoice email skipped: user has no email (order %s)", order.id)
        return
    
    if not hasattr(order, "invoice") or not order.invoice or not order.invoice.pdf:
        log.warning("Invoice email skipped: no invoice file yet (order %s)", order.id)
        return

    ctx = {
        "order": order,
        "orders_url": FRONTEND_ORDERS_URL,
        "created_at_display": localtime(order.created_at).strftime("%Y-%m-%d %H:%M"),
        "payment_method_display": _payment_method_label(order),
        "total_display": _fmt(order.total_price),
        "now": timezone.now(),
    }
    inv_num = getattr(getattr(order, "invoice", None), "number", None) or getattr(order, "invoice_number", None) or order.number
    subject = f"Your invoice {inv_num} / Ihre Rechnung {inv_num}"

    # bilingual bodies (fallbacks)
    text_en = (
        f"Thank you for your purchase!\n\n"
        f"Order: {order.number}\nTotal: {ctx['total_display']}\nPlaced at: {ctx['created_at_display']}\n\n"
        f"View order: {ctx['orders_url']}\n"
    )
    text_de = (
        f"Vielen Dank für Ihren Einkauf!\n\n"
        f"Bestellung: {order.number}\nGesamt: {ctx['total_display']}\nBestellt am: {ctx['created_at_display']}\n\n"
        f"Bestellung ansehen: {ctx['orders_url']}\n"
    )
    text = "ENGLISH\n" + text_en + "\nDEUTSCH\n" + text_de

    html_en = (
        f"<p>Thank you for your purchase!</p>"
        f"<p><b>Order:</b> {order.number}<br><b>Total:</b> {ctx['total_display']}<br>"
        f"<b>Placed at:</b> {ctx['created_at_display']}</p>"
        f"<p><a href='{ctx['orders_url']}'>View your order</a></p>"
    )
    html_de = (
        f"<p>Vielen Dank für Ihren Einkauf!</p>"
        f"<p><b>Bestellung:</b> {order.number}<br><b>Gesamt:</b> {ctx['total_display']}<br>"
        f"<b>Bestellt am:</b> {ctx['created_at_display']}</p>"
        f"<p><a href='{ctx['orders_url']}'>Bestellung ansehen</a></p>"
    )
    html = f"<div style='font-family:sans-serif'><h3>ENGLISH</h3>{html_en}<hr/><h3>DEUTSCH</h3>{html_de}</div>"

    msg = EmailMultiAlternatives(
        subject=subject,
        body=text,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[to_email],
        reply_to=getattr(settings, "ORDER_EMAIL_REPLY_TO", []),
    )

    # Try to attach invoice if present in any known place
    attached = False
    try:
        inv = getattr(order, "invoice", None)
        if inv and getattr(inv, "pdf", None):
            try:
                content = inv.pdf.read()
                msg.attach(f"{getattr(inv, 'number', inv_num)}.pdf", content, "application/pdf")
                attached = True
            except Exception:
                pass
    except Exception:
        pass

    if not attached:
        try:
            attach_invoice_if_available(msg, order)  # your helper tries invoice_pdf/path
            attached = True
        except Exception:
            pass

    # Inline logo + HTML part
    try:
        cid = _attach_inline_logo(msg)  # optional; your HTML doesn’t require it
    except Exception:
        pass

    # IMPORTANT while debugging:
    log.info("EMAIL: sending invoice '%s' to %s (attached=%s)", subject, to_email, attached)
    msg.attach_alternative(html, "text/html")
    msg.send(fail_silently=False)
    log.info("EMAIL: sent invoice '%s' to %s OK", subject, to_email)

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








