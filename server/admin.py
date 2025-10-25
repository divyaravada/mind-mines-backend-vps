# server/admin.py
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.utils.html import format_html, format_html_join
from django.conf import settings

from .models import (
    Address,
    ContactMessage,
    Coupon,
    Invoice,
    NaturalStone,
    NaturalStoneImage,
    Order,
    OrderItem,
    Product,
    ProductMedia,
    Profile,
    Promotion,
    ReturnAttachment,
    ReturnRequest,
    Review,
    StockAlert,
    StoneEnquiry,
    User,
    Wishlist,
    NewsletterSubscriber
)
from .utils.invoices import generate_invoice_pdf
from .utils.emails import (
    send_cancellation_email,
    send_invoice_email,
    send_shipping_update,
)
from .utils.background import fire_and_forget
from .tasks import post_payment_pipeline, send_user_birthday_coupon



# Optional utilities (if you have them; otherwise the admin will still work without)
try:
    from .utils.invoices import generate_invoice_pdf
except Exception:
    def generate_invoice_pdf(*args, **kwargs):
        return None

try:
    from .utils.emails import send_invoice_email, send_shipping_update, send_cancellation_email
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
    # compute from items as a last resort
    try:
        total = 0
        for oi in o.items.all():
            # prefer a method if it exists
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
        # Common fields: product_name or product.title_*
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
# Invoice
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
    # Support either .username (old) or .display_name (new)
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
        # Some schemas removed quantity; show "-" if missing
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

@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("number", "order", "created_at")
    search_fields = ("number", "order__order_number", "order__number", "order__user__email")


admin.site.register(ContactMessage)
admin.site.register(NewsletterSubscriber)
admin.site.register(Review)
admin.site.register(StoneEnquiry)
admin.site.register(Wishlist)
admin.site.register(Promotion)
admin.site.register(Coupon)

