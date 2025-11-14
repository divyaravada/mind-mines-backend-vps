# server/utils/invoices.py
from io import BytesIO
from typing import Any, List, Union, cast, Sequence
from decimal import Decimal
from django.core.files.base import ContentFile
from django.contrib.staticfiles import finders
from django.utils.timezone import localtime

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Flowable
from reportlab.lib.styles import getSampleStyleSheet

from pdfrw import PdfReader, PdfWriter, PageMerge

from server.models import Invoice

def _eur(v: Any) -> str:
    d = Decimal(str(v))
    return f"{d:.2f} €"


def _build_invoice_content(order: Any, lang: str = "en") -> bytes:
    """Build a single-page invoice content in the given language (no background)."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        topMargin=50 * mm,
        bottomMargin=18 * mm,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        title=f"Invoice {getattr(order, 'invoice_number', None) or getattr(order, 'number', '')}",
    )

    styles = getSampleStyleSheet()
    H1 = styles["Heading1"]; H1.fontName = "Helvetica-Bold"; H1.fontSize = 18; H1.spaceAfter = 6
    H2 = styles["Heading2"]; H2.fontName = "Helvetica-Bold"; H2.fontSize = 11; H2.spaceBefore = 6; H2.spaceAfter = 6
    N  = styles["Normal"];   N.fontName  = "Helvetica";       N.fontSize  = 10; N.leading = 14

    created = localtime(order.created_at).strftime("%Y-%m-%d")

    # Static texts per language
    if lang == "de":
        title_text = "RECHNUNG"
        from_label = "Von"
        bill_to_label = "Rechnung an"
        details_label = "Details"
        item_col = "Artikel"
        qty_col = "Menge"
        unit_col = "Einzelpreis"
        total_col = "Gesamt"
        subtotal_label = "Zwischensumme"
        shipping_label = "Versand"
        total_label = "Gesamtbetrag"
        free_shipping_text = "Kostenlos"
        thankyou_text = (
            "Vielen Dank für Ihren Einkauf. Wenn Sie per Banküberweisung bezahlt haben, "
            "geben Sie bitte die Referenznummer aus Ihrer Bestellbestätigung an. "
            "Rückgaben sind innerhalb von 15 Tagen im Originalzustand möglich."
        )
        invoice_no_label = "Rechnungs-Nr."
        order_no_label = "Bestell-Nr."
        date_label = "Datum"
        payment_method_label = "Zahlungsart"
        status_label_txt = "Status"
    else:
        title_text = "INVOICE"
        from_label = "From"
        bill_to_label = "Bill to"
        details_label = "Details"
        item_col = "Item"
        qty_col = "Qty"
        unit_col = "Unit"
        total_col = "Total"
        subtotal_label = "Subtotal"
        shipping_label = "Shipping"
        total_label = "Total"
        free_shipping_text = "Free"
        thankyou_text = (
            "Thank you for your purchase. If you paid by bank transfer, please include the "
            "reference number from your order confirmation. Returns accepted within 15 days "
            "in original condition."
        )
        invoice_no_label = "Invoice no."
        order_no_label = "Order no."
        date_label = "Date"
        payment_method_label = "Payment method"
        status_label_txt = "Status"

    # Company block – adjust if you want to pull from settings
    company_lines: List[str] = [
        "<b>MindMines GbR</b>",
        "Feldstr. 11",
        "21640 Horneburg, Germany",
        "Mobile: +49 170 4658 188 <br/>",
        "Email: info@mindmines.eu <br/>"
        "www.mindmines.eu",
    ]

    # Bill-to (prefers new fields, falls back to legacy)
    a = order.shipping_address
    full_name = (
        (" ".join([getattr(a, "first_name", "") or "", getattr(a, "last_name", "") or ""]).strip())
        or getattr(a, "full_name", "")
        or getattr(getattr(order, "user", None), "email", "")
    )
    line1 = getattr(a, "address_line1", "") or (
        f"{getattr(a, 'street_name','')} {getattr(a,'house_number','')}".strip()
    )
    city_line = f"{getattr(a,'postal_code', getattr(a,'pincode',''))} {getattr(a,'city', getattr(a,'state',''))}".strip()

    bill_lines_raw: List[str] = [
        f"<b>{full_name}</b>",
        line1,
        (getattr(a, "address_line2", "") or getattr(a, "apartment", "") or "").strip(),
        city_line,
        getattr(a, "country", "") or "",
        (f"Phone: {getattr(a,'phone')}" if getattr(a, "phone", "") else ""),
    ]
    bill_lines = [x for x in bill_lines_raw if x]

    # Meta block
    status_label = str(order.status).upper()
    meta_lines: List[str] = [
        f"{invoice_no_label}: <b>{order.invoice_number or '-'}</b>",
        f"{order_no_label}: <b>{order.number}</b>",
        f"{date_label}: <b>{created}</b>",
        f"{payment_method_label}: <b>{order.payment_method.title()}</b>",
        f"{status_label_txt}: <b>{status_label}</b>",
    ]

    elems: List[Flowable] = []
    elems.append(Paragraph(title_text, H1))
    elems.append(Spacer(1, 4))

    # Header + meta grid (2 columns)
    meta_tbl = Table(
        [
            [Paragraph(f"<b>{from_label}</b>", N), Paragraph(f"<b>{bill_to_label}</b>", N)],
            [Paragraph("<br/>".join(company_lines), N), Paragraph("<br/>".join(bill_lines), N)],
            ["", ""],
            [Paragraph(f"<b>{details_label}</b>", N), ""],
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
    row0: List[Union[str, Paragraph]] = [item_col, qty_col, unit_col, total_col]
    data: List[List[Union[str, Paragraph]]] = [row0]
    for oi in order.items.all():  # type: ignore[attr-defined]
        data.append(
            [
                Paragraph(str(oi.product_name)[:80], N),
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
    ship_display = (
        free_shipping_text
        if not getattr(order, "shipping_price", None) or Decimal(order.shipping_price) == 0
        else _eur(order.shipping_price)
    )

    def bold(text: str) -> str:
        return f"{text}"

    totals_tbl = Table(
        [
            [subtotal_label, _eur(order.subtotal)],
            [shipping_label, ship_display],
            ["", ""],
            [total_label, bold(f"{_eur(order.total_price)}")],
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
    note = Paragraph(thankyou_text, N)
    elems.append(note)

    # Build
    doc.build(elems)
    return buf.getvalue()


def generate_invoice_pdf(order):
    """
    Idempotent. If an invoice with a file exists, returns it.
    Otherwise, builds a polished content PDF in German (page 1)
    and English (page 2), optionally overlaid on a static template.
    """
    # Reuse if already generated
    inv = getattr(order, "invoice", None)
    if inv and inv.pdf:
        return inv

    # Ensure invoice number
    order.assign_unique_invoice_number()
    number = order.invoice_number

    # Build content for both languages
    content_de_bytes = _build_invoice_content(order, lang="de")
    content_en_bytes = _build_invoice_content(order, lang="en")

    # Read as PDFs
    de_reader = PdfReader(BytesIO(content_de_bytes))  # type: ignore[call-arg]
    en_reader = PdfReader(BytesIO(content_en_bytes))  # type: ignore[call-arg]

    pages: List[Any] = []
    pages.extend(getattr(de_reader, "pages", []) or [])
    pages.extend(getattr(en_reader, "pages", []) or [])

    pdf_bytes = b""
    template_path = finders.find("pdf/Mindmines template.pdf")
    writer = PdfWriter()
    out_buf = BytesIO()

    if template_path and pages:
        # For each page, overlay on a fresh copy of the first template page
        try:
            for overlay_page in pages:
                tpl = PdfReader(template_path)  # fresh reader each time
                tpl_pages: Sequence[Any] = cast(Sequence[Any], getattr(tpl, "pages", []) or [])
                if not tpl_pages:
                    # no pages in template, just use overlay
                    writer.addpage(overlay_page)
                    continue

                base_page = tpl_pages[0]
                PageMerge(base_page).add(overlay_page).render()
                writer.addpage(base_page)
        except Exception:
            # Fallback: no template overlay, just plain pages
            writer = PdfWriter()
            for p in pages:
                writer.addpage(p)
    else:
        # No template, just use the content pages
        for p in pages:
            writer.addpage(p)

    writer.write(out_buf)
    pdf_bytes = out_buf.getvalue()

    # Save to FileField via Invoice model
    invoice = inv or Invoice(order=order, number=number)
    invoice.pdf.save(f"{number}.pdf", ContentFile(pdf_bytes), save=True)

    # Mirror number on order if needed
    if order.invoice_number != number:
        type(order).objects.filter(pk=order.pk).update(invoice_number=number)

    return invoice
