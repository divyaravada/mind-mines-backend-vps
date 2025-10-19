# server/utils/invoices.py
from io import BytesIO
from decimal import Decimal
from django.core.files.base import ContentFile
from django.contrib.staticfiles import finders
from django.utils.timezone import localtime

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet

from pdfrw import PdfReader, PdfWriter, PageMerge

from server.models import Invoice


def _eur(v) -> str:
    d = Decimal(str(v))
    return f"{d:.2f} €"


def _build_invoice_content(order) -> bytes:
    """Build a nice-looking single-page invoice content (no background)."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        topMargin=50 * mm,
        bottomMargin=18 * mm,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        title=f"Invoice {order.invoice_number or order.number}",
    )

    styles = getSampleStyleSheet()
    H1 = styles["Heading1"]; H1.fontName = "Helvetica-Bold"; H1.fontSize = 18; H1.spaceAfter = 6
    H2 = styles["Heading2"]; H2.fontName = "Helvetica-Bold"; H2.fontSize = 11; H2.spaceBefore = 6; H2.spaceAfter = 6
    N  = styles["Normal"];   N.fontName  = "Helvetica";       N.fontSize  = 10; N.leading = 14

    created = localtime(order.created_at).strftime("%Y-%m-%d")

    # Company block – adjust if you want to pull from settings
    company_lines = [
        "<b>MindMines GbR</b>",
        "Feldstr. 11",
        "21640 Horneburg, Germany",
        "Mobile: +49 170 4658 188 <br/>",
        "Email: info@mindmines.eu <br/>"
        "• www.mindmines.eu",
    ]

    # Bill-to (prefers new fields, falls back to legacy)
    a = order.shipping_address
    full_name = (" ".join([getattr(a, "first_name", "") or "", getattr(a, "last_name", "") or ""]).strip()
                 or getattr(a, "full_name", "") or order.user.email)
    line1 = getattr(a, "address_line1", "") or (f"{getattr(a, 'street_name','')} {getattr(a,'house_number','')}".strip())
    city_line = f"{getattr(a,'postal_code', getattr(a,'pincode',''))} {getattr(a,'city', getattr(a,'state',''))}".strip()

    bill_lines = [
        f"<b>{full_name}</b>",
        line1,
        (getattr(a, "address_line2", "") or getattr(a, "apartment", "") or "").strip(),
        city_line,
        getattr(a, "country", "") or "",
        getattr(a, "phone", "") and f"Phone: {getattr(a,'phone')}" or "",
    ]
    bill_lines = [x for x in bill_lines if x]

    # Meta block
    status_label = order.status.upper()
    meta_lines = [
        f"Invoice no.: <b>{order.invoice_number or '-'}</b>",
        f"Order no.: <b>{order.number}</b>",
        f"Date: <b>{created}</b>",
        f"Payment method: <b>{order.payment_method.title()}</b>",
        f"Status: <b>{status_label}</b>",
    ]

    # Header + meta grid (2 columns)
    elems = []
    elems.append(Paragraph("INVOICE", H1))
    elems.append(Spacer(1, 4))

    meta_tbl = Table(
        [
            [Paragraph("<b>From</b>", N), Paragraph("<b>Bill to</b>", N)],
            [Paragraph("<br/>".join(company_lines), N), Paragraph("<br/>".join(bill_lines), N)],
            ["", ""],
            [Paragraph("<b>Details</b>", N), ""],
            [Paragraph("<br/>".join(meta_lines), N), ""],
        ],
        colWidths=[90 * mm, 90 * mm],
        hAlign="LEFT",
    )
    meta_tbl.setStyle(
        TableStyle(
            [
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING",    (0, 0), (-1, -1), 8),
                ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    elems.append(meta_tbl)
    elems.append(Spacer(1, 8))

    # Line items
    data = [["Item", "Qty", "Unit", "Total"]]
    for oi in order.items.all():
        data.append(
            [
                Paragraph(oi.product_name[:80], N),
                str(oi.quantity),
                _eur(oi.unit_price),
                _eur(oi.line_total()),
            ]
        )

    items_tbl = Table(
        data,
        colWidths=[90 * mm, 20 * mm, 30 * mm, 30 * mm],
        repeatRows=1,
        hAlign="LEFT",
    )
    items_tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
                ("TEXTCOLOR",  (0, 0), (-1, 0), colors.HexColor("#111827")),
                ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE",   (0, 0), (-1, 0), 10),
                ("ALIGN",      (1, 1), (-1, -1), "RIGHT"),
                ("ALIGN",      (0, 1), (0, -1), "LEFT"),
                ("GRID",       (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FAFAFA")]),
            ]
        )
    )
    elems.append(items_tbl)
    elems.append(Spacer(1, 10))

    # Totals box
    ship_display = "Free" if not order.shipping_fee or Decimal(order.shipping_fee) == 0 else _eur(order.shipping_fee)
    def bold(text):
        return f'''{text}'''

    totals_tbl = Table(
        [
            ["Subtotal", _eur(order.subtotal)],
            ["Shipping", ship_display],
            ["", ""],
            ['Total', bold(f"{_eur(order.total_price)}")],
        ],
        colWidths=[120 * mm, 50 * mm],
        hAlign="RIGHT",
        style=TableStyle(
            [
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("LINEABOVE", (0, 3), (-1, 3), 0.6, colors.black),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("FONTNAME", (0, 3), (-1, 3), "Helvetica-Bold"),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                
            ]
        ),
    )
    elems.append(totals_tbl)
    elems.append(Spacer(1, 10))

    # Notes
    note = Paragraph(
        "Thank you for your purchase. If you paid by bank transfer, please include the "
        "reference number from your order confirmation. Returns accepted within 15 days in original condition.",
        N,
    )
    elems.append(note)

    # Build
    doc.build(elems)
    return buf.getvalue()


def generate_invoice_pdf(order):
    """
    Idempotent. If an invoice with a file exists, returns it.
    Otherwise, builds a polished content PDF and overlays it onto
    static/pdf/Mindmines template.pdf when available.
    """
    # Reuse if already generated
    if hasattr(order, "invoice") and order.invoice and order.invoice.pdf:
        return order.invoice

    # Ensure invoice number
    order.assign_unique_invoice_number()
    number = order.invoice_number

    # Build content
    content_bytes = _build_invoice_content(order)

    # Try to overlay on brand template (optional)
    pdf_bytes = content_bytes
    template_path = finders.find("pdf/Mindmines template.pdf")
    if template_path:
        try:
            tpl = PdfReader(template_path)
            overlay_reader = PdfReader(BytesIO(content_bytes))

            # Merge first page (single-page invoice)
            base_page = tpl.pages[0]
            overlay_page = overlay_reader.pages[0]
            PageMerge(base_page).add(overlay_page).render()

            out = BytesIO()
            writer = PdfWriter()
            writer.addpage(base_page)
            writer.write(out)
            pdf_bytes = out.getvalue()
        except Exception:
            # If anything goes wrong, fall back to plain content
            pdf_bytes = content_bytes

    # Save to FileField via Invoice model
    invoice = getattr(order, "invoice", None) or Invoice(order=order, number=number)
    invoice.pdf.save(f"{number}.pdf", ContentFile(pdf_bytes), save=True)

    # Mirror number on order if needed
    if order.invoice_number != number:
        order.invoice_number = number
        order.save(update_fields=["invoice_number"])

    return invoice
