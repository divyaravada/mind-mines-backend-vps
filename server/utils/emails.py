
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
    # Fallback to your manual mapping (covers unusual codes)
    return _payment_method_label(getattr(order, "payment_method", ""))


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
    print(order)
    print(total)
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
def send_bank_transfer_instructions(order, bank, user_locale="en"):
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
        }

        subject = f"Bank transfer instructions for order {order.number}"
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
        msg.send(fail_silently=False)
    finally:
        activate(prior_lang)


# ------------------ INVOICE (PAID) ------------------
def send_invoice_email(order):
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
    subject = f"Your invoice {order.invoice.number}"
    to = [to_email]

    # Plain text
    text = render_to_string("emails/invoice.txt", ctx)
    msg = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, to)

    # Attach PDF
    msg.attach(f"{order.invoice.number}.pdf", order.invoice.pdf.read(), "application/pdf")

    # Inline logo, then HTML
    cid = _attach_inline_logo(msg)
    if cid:
        ctx["logo_cid"] = cid

    html = render_to_string("emails/invoice.html", ctx)
    msg.attach_alternative(html, "text/html")
    msg.send(fail_silently=False)

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








