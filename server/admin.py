# server/admin.py
from django.contrib import admin
from django.utils.html import format_html_join, format_html
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import (
    Product, Wishlist, ProductMedia, NaturalStone, NaturalStoneImage,
    StoneEnquiry, User, Profile, ContactMessage, NewsletterSubscriber, Coupon, StockAlert,
    Address, Order, Review, OrderItem, Invoice, ReturnRequest, ReturnAttachment, Promotion
)

# --------------------------------------------------------------------------------------
# Optional utilities (safe fallbacks if utils are missing)
# --------------------------------------------------------------------------------------
try:
    from .utils.invoices import generate_invoice_pdf  # noqa
except Exception:
    def generate_invoice_pdf(*args, **kwargs):
        return None

try:
    from .utils.emails import send_invoice_email, send_shipping_update, send_cancellation_email  # noqa
except Exception:
    def send_invoice_email(*args, **kwargs): pass
    def send_shipping_update(*args, **kwargs): pass
    def send_cancellation_email(*args, **kwargs): pass

# --------------------------------------------------------------------------------------
# Helpers that safely read possibly-renamed fields
# --------------------------------------------------------------------------------------
def get_order_number(o):
    """Works with either .order_number (new) or .number (old)."""
    return getattr(o, "order_number", None) or getattr(o, "number", None) or "-"

def get_canceled_at(o):
    """Works with .canceled_at (new) or .cancelled_at (old)."""
    return getattr(o, "canceled_at", None) or getattr(o, "cancelled_at", None)

def get_total(o):
    """
    Try several common total fields; fall back to computing from items.
    Newer schema may have .total_gross; older had .total_price; some use .total.
    """
    for name in ("total_price", "total_gross", "total", "grand_total"):
        val = getattr(o, name, None)
        if val is not None:
            return val
    try:
        total = 0
        for oi in o.items.all():
            if hasattr(oi, "line_total") and callable(getattr(oi, "line_total")):
                total += oi.line_total()
            else:
                unit = getattr(oi, "unit_price_gross", None)
                if unit is None:
                    unit = getattr(oi, "unit_price", 0)
                qty = getattr(oi, "quantity", 1)
                total += (unit or 0) * qty
        return total
    except Exception:
        return None

def fmt_eur(v):
    try:
        return f"{v:.2f} €"
    except Exception:
        return "-"

# --------------------------------------------------------------------------------------
# Inline for order items
# --------------------------------------------------------------------------------------
class OrderItemInline(admin.TabularInline):
    model = OrderItem
    fk_name = "order"
    extra = 0
    can_delete = False

    readonly_fields = ("item_name", "quantity", "unit_price_display", "line_total_display")
    fields = ("item_name", "quantity", "unit_price_display", "line_total_display")

    def has_add_permission(self, request, obj=None):
        return False

    def item_name(self, obj):
        name = getattr(obj, "product_name", None)
        if name:
            return name
        prod = getattr(obj, "product", None)
        if prod:
            return getattr(prod, "title_en", None) or getattr(prod, "title_de", None) or str(prod)
        return "-"
    item_name.short_description = "Item"

    def unit_price_display(self, obj):
        unit = getattr(obj, "unit_price_gross", None)
        if unit is None:
            unit = getattr(obj, "unit_price", None)
        return fmt_eur(unit) if unit is not None else "-"
    unit_price_display.short_description = "Unit price"

    def line_total_display(self, obj):
        try:
            if hasattr(obj, "line_total") and callable(getattr(obj, "line_total")):
                return fmt_eur(obj.line_total())
            unit = getattr(obj, "unit_price_gross", None)
            if unit is None:
                unit = getattr(obj, "unit_price", 0)
            qty = getattr(obj, "quantity", 1)
            return fmt_eur((unit or 0) * qty)
        except Exception:
            return "-"
    line_total_display.short_description = "Line total"

# --------------------------------------------------------------------------------------
# Order admin
# --------------------------------------------------------------------------------------
@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    date_hierarchy = "created_at"
    list_display = ("order_number_col", "user", "status", "total_col", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("order_number", "number", "user__email")  # support old + new

    actions = []

    readonly_fields = (
        "order_number_col",
        "created_at",
        "updated_at",
        "paid_at_safe",
        "canceled_at_safe",
        "total_col",
        "subtotal_col",
        "vat_col",
        "items_readonly",
        "stripe_refs",
        "shipping_address_display",
        "billing_address_display",
    )

    fields = (
        ("order_number_col", "user", "created_at", "paid_at_safe", "canceled_at_safe"),
        ("status", "payment_method"),
        ("tracking_number",),
        ("subtotal_col", "vat_col", "total_col"),
        "shipping_address_display",
        "billing_address_display",
        "items_readonly",
        ("stripe_refs",),
        "remarks",
    )

    inlines = [OrderItemInline]

    # ----- Display helpers -----

    def order_number_col(self, obj):
        return get_order_number(obj)
    order_number_col.short_description = "Order #"

    def total_col(self, obj):
        return fmt_eur(get_total(obj))
    total_col.short_description = "Total"

    def subtotal_col(self, obj):
        val = getattr(obj, "subtotal", None)
        if val is None:
            val = getattr(obj, "subtotal_gross", None)
        return fmt_eur(val) if val is not None else "-"
    subtotal_col.short_description = "Subtotal"

    def vat_col(self, obj):
        val = getattr(obj, "vat_amount", None)
        return fmt_eur(val) if val is not None else "-"
    vat_col.short_description = "VAT"

    def paid_at_safe(self, obj):
        return getattr(obj, "paid_at", None)
    paid_at_safe.short_description = "Paid at"

    def canceled_at_safe(self, obj):
        # supports canceled_at (new) / cancelled_at (old)
        return get_canceled_at(obj)
    canceled_at_safe.short_description = "Canceled at"

    def stripe_refs(self, obj):
        bits = []
        for label, attr in (
            ("PI", "stripe_payment_intent"),
            ("Customer", "stripe_customer"),
            ("SEPA mandate", "sepa_mandate_id"),
            ("Payment ID", "payment_id"),         # old
            ("Payment Ref", "payment_ref"),       # old
        ):
            val = getattr(obj, attr, "") or ""
            if val:
                bits.append(f"<div><b>{label}:</b> {val}</div>")
        return format_html("".join(bits)) if bits else "-"
    stripe_refs.allow_tags = True
    stripe_refs.short_description = "Stripe/Payment"

    # ----- Address renderers (support JSON or old FK) -----

    def _render_address_dict(self, d: dict) -> str:
        # Flexible keys across schemas
        def g(*names, default=""):
            for n in names:
                if d.get(n):
                    return d.get(n)
            return default

        lines = [
            g("full_name", default="").strip() or
            (" ".join([g("first_name",""), g("last_name","")]).strip()),
            g("address_line1") or " ".join([g("street_name",""), g("house_number","")]).strip(),
            g("address_line2") or g("apartment",""),
            " ".join([g("postal_code","pincode",""), g("city","state","")]).strip(),
            g("country",""),
            (f"Phone: {g('phone','')}" if g("phone","") else ""),
        ]
        lines = [x for x in lines if x]
        if not lines:
            return "-"
        return format_html("<div>{}</div>", format_html_join("", "<div>{}</div>", [(l,) for l in lines]))

    def _render_address_model(self, a) -> str:
        if not a:
            return "-"
        parts = [
            getattr(a, "full_name", "") or " ".join([getattr(a, "first_name", "") or "", getattr(a, "last_name", "") or ""]).strip(),
            getattr(a, "address_line1", "") or " ".join([getattr(a, "street_name", "") or "", getattr(a, "house_number", "") or ""]).strip(),
            getattr(a, "address_line2", "") or getattr(a, "apartment", "") or "",
            " ".join([
                getattr(a, "postal_code", "") or getattr(a, "pincode", "") or "",
                getattr(a, "city", "") or getattr(a, "state", "") or "",
            ]).strip(),
            getattr(a, "country", "") or "",
            (f"Phone: {getattr(a, 'phone')}" if getattr(a, "phone", "") else ""),
        ]
        parts = [x for x in parts if x]
        if not parts:
            return "-"
        return format_html("<div>{}</div>", format_html_join("", "<div>{}</div>", [(p,) for p in parts]))

    def shipping_address_display(self, obj):
        d = getattr(obj, "shipping_address_json", None)
        if isinstance(d, dict) and d:
            return self._render_address_dict(d)
        # fallback to old FK
        a = getattr(obj, "shipping_address", None)
        return self._render_address_model(a)
    shipping_address_display.short_description = "Shipping address"

    def billing_address_display(self, obj):
        d = getattr(obj, "billing_address_json", None)
        if isinstance(d, dict) and d:
            return self._render_address_dict(d)
        a = getattr(obj, "billing_address", None)
        return self._render_address_model(a)
    billing_address_display.short_description = "Billing address"

    # ----- Items table (unchanged logic but kept here for completeness) -----

    def items_readonly(self, obj):
        rows = []
        for oi in obj.items.all():
            if hasattr(oi, "product_name") and oi.product_name:
                title = oi.product_name
            else:
                p = getattr(oi, "product", None)
                title = (getattr(p, "title_en", None) or getattr(p, "title_de", None) or str(p)) if p else "-"
            unit = getattr(oi, "unit_price_gross", None)
            if unit is None:
                unit = getattr(oi, "unit_price", None)
            if hasattr(oi, "line_total") and callable(getattr(oi, "line_total")):
                lt = oi.line_total()
            else:
                qty = getattr(oi, "quantity", 1)
                lt = (unit or 0) * qty
            rows.append((title, f"{getattr(oi,'quantity',1)}", fmt_eur(unit or 0), fmt_eur(lt)))

        if not rows:
            return "-"

        return format_html(
            "<table style='width:100%'><tr>"
            "<th align='left'>Item</th>"
            "<th align='right'>Qty</th>"
            "<th align='right'>Unit</th>"
            "<th align='right'>Total</th></tr>{}</table>",
            format_html_join(
                "",
                "<tr><td>{}</td><td align='right'>{}</td><td align='right'>{}</td><td align='right'>{}</td></tr>",
                rows,
            ),
        )
    items_readonly.short_description = "Items"

# --------------------------------------------------------------------------------------
# Invoice (REGISTERED ONCE ONLY)
# --------------------------------------------------------------------------------------
@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("number", "order", "created_at")
    search_fields = ("number", "order__order_number", "order__number", "order__user__email")

# --------------------------------------------------------------------------------------
# User & Profile
# --------------------------------------------------------------------------------------
@admin.register(User)
class CustomUserAdmin(DjangoUserAdmin):
    model = User

    def display_name_col(self, obj):
        return getattr(obj, "display_name", None) or getattr(obj, "username", None) or obj.email
    display_name_col.short_description = "Name"

    list_display = ["email", "display_name_col", "is_active", "is_staff"]
    ordering = ["email"]
    fieldsets = (
        (None, {"fields": ("email", "password", "display_name")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "display_name", "password1", "password2", "is_staff", "is_active"),
        }),
    )
    search_fields = ("email", "display_name", "username")

@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ["user", "full_name", "date_of_birth"]
    search_fields = ("user__email", "full_name")

# --------------------------------------------------------------------------------------
# Product & Media
# --------------------------------------------------------------------------------------
class ProductMediaInline(admin.TabularInline):
    model = ProductMedia
    extra = 1
    fields = ["image", "video", "caption", "order", "media_type"]

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    inlines = [ProductMediaInline]

    def price_col(self, obj):
        val = getattr(obj, "price", None)
        if val is None:
            val = getattr(obj, "price_gross", None)
        return fmt_eur(val) if val is not None else "-"
    price_col.short_description = "Price"

    def quantity_col(self, obj):
        q = getattr(obj, "quantity", None)
        return q if q is not None else "-"
    quantity_col.short_description = "Qty"

    list_display = ["title_en", "category", "price_col", "quantity_col"]
    search_fields = ("title_en", "title_de", "category")

# --------------------------------------------------------------------------------------
# Natural stones
# --------------------------------------------------------------------------------------
class NaturalStoneImageInline(admin.TabularInline):
    model = NaturalStoneImage
    extra = 1
    max_num = 10
    fields = ["image"]
    verbose_name = "Additional Image"
    verbose_name_plural = "Upload up to 10 images"

@admin.register(NaturalStone)
class NaturalStoneAdmin(admin.ModelAdmin):
    list_display = ["type"]
    inlines = [NaturalStoneImageInline]

# --------------------------------------------------------------------------------------
# Addresses
# --------------------------------------------------------------------------------------
@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    list_display = (
        "user", "first_name", "last_name", "address_line1",
        "postal_code", "city", "country", "is_default", "created_at",
    )
    list_filter = ("country", "is_default", "created_at")
    search_fields = (
        "user__email", "first_name", "last_name", "full_name",
        "address_line1", "street_name", "postal_code", "city",
    )

# --------------------------------------------------------------------------------------
# Returns
# --------------------------------------------------------------------------------------
class ReturnAttachmentInline(admin.TabularInline):
    model = ReturnAttachment
    extra = 0

@admin.register(ReturnRequest)
class ReturnRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "user", "request_type", "status", "created_at")
    list_filter  = ("request_type", "status", "created_at")
    search_fields = ("order__order_number", "order__number", "order__id", "user__email")
    date_hierarchy = "created_at"
    inlines = [ReturnAttachmentInline]

# --------------------------------------------------------------------------------------
# Misc simple models
# --------------------------------------------------------------------------------------
@admin.register(StockAlert)
class StockAlertAdmin(admin.ModelAdmin):
    list_display = ("product", "email", "is_active", "created_at", "notified_at")
    list_filter = ("is_active", "created_at", "notified_at")
    search_fields = ("email", "product__title_en", "product__title_de")

admin.site.register(ContactMessage)
admin.site.register(NewsletterSubscriber)
admin.site.register(Review)
admin.site.register(StoneEnquiry)
admin.site.register(Wishlist)
admin.site.register(Promotion)
admin.site.register(Coupon)
