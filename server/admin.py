# server/admin.py
from django.contrib import admin
from django.utils.html import format_html_join, format_html
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from typing import Any, Optional
from .models import (
    Product, Wishlist, ProductMedia, NaturalStone, NaturalStoneImage,
    StoneEnquiry, User, Profile, ContactMessage, NewsletterSubscriber, Coupon, StockAlert,
    Address, Order, Review, OrderItem, Invoice, ReturnRequest, ReturnAttachment, Promotion
)
from django.contrib import messages

# --------------------------------------------------------------------------------------
# Optional utilities (safe fallbacks if utils are missing)
# --------------------------------------------------------------------------------------

def generate_invoice_pdf(*args, **kwargs):
    return None  # default no-op

def send_invoice_email(*args, **kwargs):
    return None  # default no-op

def send_shipping_update(*args, **kwargs):
    return None  # default no-op

def send_cancellation_email(*args, **kwargs):
    return None

try:
    from .utils.invoices import generate_invoice_pdf as _gen_inv_pdf
    generate_invoice_pdf = _gen_inv_pdf  
except (ImportError, ModuleNotFoundError, AttributeError):
    pass

try:
    from .utils.emails import (  
        send_invoice_email as _send_invoice_email,
        send_shipping_update as _send_shipping_update,
        send_cancellation_email as _send_cancellation_email,
    )
    send_invoice_email = _send_invoice_email     
    send_shipping_update = _send_shipping_update  
    send_cancellation_email = _send_cancellation_email 
except (ImportError, ModuleNotFoundError, AttributeError):
    pass

# --------------------------------------------------------------------------------------
# Helpers that safely read possibly-renamed fields
# --------------------------------------------------------------------------------------
def get_order_number(o: Any) -> str:
    if hasattr(o, "order_number") and getattr(o, "order_number") is not None:
        return str(getattr(o, "order_number"))
    if hasattr(o, "number") and getattr(o, "number") is not None:
        return str(getattr(o, "number"))
    return "-"

def get_canceled_at(o: Any):
    return getattr(o, "canceled_at", None) or getattr(o, "cancelled_at", None)

def get_total(o: Any):
    for name in ("total_price", "total_gross", "total", "grand_total"):
        if hasattr(o, name):
            val = getattr(o, name)
            if val is not None:
                return val
            
    items_mgr = getattr(o, "items", None)
    if not hasattr(items_mgr, "all"):
        return None

    total = 0
    for oi in items_mgr.all():
        if hasattr(oi, "line_total") and callable(oi.line_total):
            lt = oi.line_total()
            total += lt if lt is not None else 0
        else:
            unit = getattr(oi, "unit_price_gross", None)
            if unit is None:
                unit = getattr(oi, "unit_price", 0)
            qty = getattr(oi, "quantity", 1) or 1
            total += (unit or 0) * qty
    return total

def fmt_eur(v: Optional[Any]) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):.2f} €"
    except (TypeError, ValueError):
        return "-"
    
# ----- actions -----
@admin.action(description="Mark selected orders as PAID (+ send invoice)")
def mark_paid(modeladmin, request, queryset):
    updated = 0
    for order in queryset:
        if getattr(order, "status", None) == "paid":
            continue
        order.status = "paid"
        order.save(update_fields=["status"])
        generate_invoice_pdf(order)  
        user_locale = getattr(getattr(order.user, "profile", None), "language", "en") or "en"
        send_invoice_email(order, user_locale=user_locale) 
        updated += 1
    modeladmin.message_user(request, f"Marked {updated} order(s) as paid and sent invoice.", level=messages.SUCCESS)

@admin.action(description="Mark selected orders as SHIPPED (+ email)")
def mark_shipped(modeladmin, request, queryset):
    n = 0
    for order in queryset:
        order.status = "shipped"
        order.save(update_fields=["status"])
        send_shipping_update(order, tracking=getattr(order, "tracking_number", "") or "")  
        n += 1
    modeladmin.message_user(request, f"Marked {n} order(s) as shipped and sent emails (if configured).", level=messages.SUCCESS)

@admin.action(description="Cancel selected orders (+ notify customer)")
def mark_cancelled(modeladmin, request, queryset):
    n = 0
    for order in queryset:
        prev = getattr(order, "status", None)
        order.status = "cancelled"
        order.save(update_fields=["status"])
        if prev != "cancelled" and hasattr(order, "release_stock") and callable(order.release_stock):
            order.release_stock()
        send_cancellation_email(order)  
        n += 1
    modeladmin.message_user(request, f"Cancelled {n} order(s) and notified customers (if configured).", level=messages.SUCCESS)

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
        if hasattr(obj, "product_name") and obj.product_name:
            return obj.product_name
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
        if hasattr(obj, "line_total") and callable(obj.line_total):
            try:
                return fmt_eur(obj.line_total())
            except (TypeError, ValueError):
                pass
        unit = getattr(obj, "unit_price_gross", None)
        if unit is None:
            unit = getattr(obj, "unit_price", 0)
        qty = getattr(obj, "quantity", 1) or 1
        return fmt_eur((unit or 0) * qty)
    line_total_display.short_description = "Line total"

# --------------------------------------------------------------------------------------
# Order admin
# --------------------------------------------------------------------------------------
@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    date_hierarchy = "created_at"
    list_display = ("number", "user", "customer_email", "status", "total_price_display", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("number", "user__email", "email")
    actions = [mark_paid, mark_shipped, mark_cancelled]

    readonly_fields = (
        # model fields you want read-only in admin
        "number", "subtotal", "discount_amount", "shipping_fee", "total_price",
        "payment_ref", "payment_id", "created_at", "updated_at",
        "items_readonly", "delivered_at", "cancelled_at",
        # IMPORTANT: include any non-model “display” methods that appear in `fields`
        "customer_email",
    )

    fields = (
        ("number", "user", "customer_email", "created_at", "delivered_at", "cancelled_at"),
        ("status", "payment_method", "tracking_number"),
        ("subtotal", "discount_amount", "shipping_fee", "total_price"),
        ("shipping_address",),
        "items_readonly",
        ("payment_ref", "payment_id"),
    )

    inlines = [OrderItemInline]

    # ---- list_display helpers ----
    def customer_email(self, obj):
        return getattr(obj, "email", None) or getattr(getattr(obj, "user", None), "email", None) or "-"
    customer_email.short_description = "Email"

    def total_price_display(self, obj):
        val = getattr(obj, "total_price", None)
        if val is None:
            return "-"
        try:
            return f"{float(val):.2f} €"
        except (TypeError, ValueError):
            return str(val)

    # ---- readonly_fields helpers (names match readonly_fields above) ----
    def subtotal(self, obj):
        return f"{float(getattr(obj, 'subtotal', 0)):.2f} €"

    def discount_amount(self, obj):
        return f"{float(getattr(obj, 'discount_amount', 0)):.2f} €"

    def shipping_fee(self, obj):
        return f"{float(getattr(obj, 'shipping_fee', 0)):.2f} €"

    def total_price(self, obj):
        return f"{float(getattr(obj, 'total_price', 0)):.2f} €"

    def payment_ref(self, obj):
        return getattr(obj, "payment_ref", "") or "-"

    def payment_id(self, obj):
        return getattr(obj, "payment_id", "") or "-"

    def delivered_at(self, obj):
        return getattr(obj, "delivered_at", None)

    def cancelled_at(self, obj):
        return getattr(obj, "cancelled_at", None)

    def items_readonly(self, obj):
        rows = []
        items = getattr(obj, "items", None)
        if hasattr(items, "all"):
            for it in items.all():
                name = getattr(it, "product_name", None) or "-"
                unit_fmt = fmt_eur(getattr(it, "unit_price", None))
                try:
                    if hasattr(it, "line_total") and callable(it.line_total):
                        total_fmt = fmt_eur(it.line_total())
                    else:
                        total_fmt = fmt_eur((getattr(it, "unit_price", 0) or 0) * (getattr(it, "quantity", 1) or 1))
                except (TypeError, ValueError):
                    total_fmt = "-"
                rows.append((f"{name} × {getattr(it, 'quantity', 1) or 1}", unit_fmt, total_fmt))

        if not rows:
            return "-"

        return format_html(
            "<table style='width:100%'><tr><th align='left'>Item</th>"
            "<th align='right'>Unit</th><th align='right'>Total</th></tr>{}</table>",
            format_html_join("", "<tr><td>{}</td><td align='right'>{}</td><td align='right'>{}</td></tr>", rows)
        )
    items_readonly.short_description = "Items"

# --------------------------------------------------------------------------------------
# Invoice (REGISTERED ONCE ONLY)
# --------------------------------------------------------------------------------------
@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("number", "order", "created_at")
    search_fields = ("number", "order__number", "order__user__email")  
    list_select_related = ("order",)
    ordering = ("-created_at",)

# --------------------------------------------------------------------------------------
# User & Profile
# --------------------------------------------------------------------------------------
@admin.register(User)
class CustomUserAdmin(DjangoUserAdmin):
    model = User
    list_display = ["email", "username", "is_active", "is_staff"]
    ordering = ["email"]
    fieldsets = (
        (None, {"fields": ("email", "password", "username")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "username", "password1", "password2", "is_staff", "is_active"),
        }),
    )
    search_fields = ("email", "username")

@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "full_name", "mobile_col", "verified_col")
    search_fields = ("user__email", "full_name")

    def mobile_col(self, obj):
        return getattr(obj, "mobile", None) or "-"
    mobile_col.short_description = "Mobile"

    def verified_col(self, obj):
        return bool(getattr(obj, "verified", False))
    verified_col.boolean = True
    verified_col.short_description = "Verified"

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
    search_fields = ("order__number", "order__id", "user__email")  
    date_hierarchy = "created_at"
    list_select_related = ("order", "user")

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
