# server/admin.py
from typing import Any, Optional

from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.utils.html import format_html_join, format_html
from django.urls import reverse

from .models import (
    Product, Wishlist, ProductMedia, NaturalStone, NaturalStoneImage, OnDemandOrderRequest,
    StoneEnquiry, User, Profile, ContactMessage, NewsletterSubscriber, Coupon, StockAlert,
    Address, Order, Review, OrderItem, Invoice, ReturnRequest, ReturnAttachment, PromotionBanner
)

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
    return None  # default no-op

try:
    from .utils.invoices import generate_invoice_pdf as _gen_inv_pdf
    generate_invoice_pdf = _gen_inv_pdf
except Exception:
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
except Exception:
    pass

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def fmt_eur(v: Optional[Any]) -> str:
    try:
        return f"{float(v):.2f} €"
    except Exception:
        return "-"

# ----- admin actions -----
@admin.action(description="Mark selected orders as PAID (+ generate/send invoice)")
def mark_paid(modeladmin, request, queryset):
    updated = 0
    for order in queryset:
        if getattr(order, "status", None) == "paid":
            continue
        order.status = "paid"
        setattr(order, "payment_status", "paid")
        order.save(update_fields=["status", "payment_status"])
        try:
            generate_invoice_pdf(order)
        except Exception:
            pass
        try:
            user_locale = getattr(getattr(order.user, "profile", None), "language", "en") or "en"
            send_invoice_email(order, user_locale=user_locale)
        except Exception:
            pass
        updated += 1
    modeladmin.message_user(
        request, f"Marked {updated} order(s) as paid and attempted to send invoice.", level=messages.SUCCESS
    )

@admin.action(description="Mark selected orders as SHIPPED (+ email)")
def mark_shipped(modeladmin, request, queryset):
    n = 0
    for order in queryset:
        order.status = "shipped"
        order.save(update_fields=["status"])
        try:
            send_shipping_update(order, tracking=getattr(order, "tracking_number", "") or "")
        except Exception:
            pass
        n += 1
    modeladmin.message_user(request, f"Marked {n} order(s) as shipped.", level=messages.SUCCESS)

@admin.action(description="Cancel selected orders (+ notify & release stock)")
def mark_cancelled(modeladmin, request, queryset):
    n = 0
    for order in queryset:
        prev = getattr(order, "status", None)
        order.status = "cancelled"
        setattr(order, "payment_status", "cancelled")
        order.save(update_fields=["status", "payment_status"])
        if prev != "cancelled" and hasattr(order, "release_stock") and callable(order.release_stock):
            try:
                order.release_stock()
            except Exception:
                pass
        try:
            send_cancellation_email(order)
        except Exception:
            pass
        n += 1
    modeladmin.message_user(request, f"Cancelled {n} order(s).", level=messages.SUCCESS)

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
        if getattr(obj, "product_name", None):
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
        return fmt_eur(unit)
    unit_price_display.short_description = "Unit price"

    def line_total_display(self, obj):
        if hasattr(obj, "line_total") and callable(obj.line_total):
            try:
                return fmt_eur(obj.line_total())
            except Exception:
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
    list_display = ("number", "user", "customer_email", "status", "total_price_col", "created_at", "invoice_link")
    list_filter = ("status", "created_at")
    search_fields = ("number", "user__email", "email")
    actions = [mark_paid, mark_shipped, mark_cancelled]
    list_select_related = ("user",)
    inlines = [OrderItemInline]

    readonly_fields = (
        "number", "subtotal_col", "discount_col", "shipping_fee_col", "total_price_col",
        "payment_ref_col", "payment_id_col", "created_at", "updated_at",
        "items_readonly", "delivered_at", "cancelled_at",
        "customer_email", "shipping_snapshot_readonly",
    )

    fields = (
        ("number", "user", "customer_email"),
        ("status", "payment_method", "tracking_number"),
        ("subtotal_col", "discount_col", "shipping_fee_col", "total_price_col"),
        ("shipping_address",),
        "shipping_snapshot_readonly",
        "items_readonly",
        ("payment_ref_col", "payment_id_col"),
        ("created_at", "delivered_at", "cancelled_at"),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related("user").prefetch_related("items", "items__product")

    # ---- list_display helpers ----
    def customer_email(self, obj):
        return getattr(obj, "email", None) or getattr(getattr(obj, "user", None), "email", None) or "-"
    customer_email.short_description = "Email"

    def total_price_col(self, obj):
        val = getattr(obj, "total_price", None)
        if val is None:
            # compute from items as fallback
            total = 0
            items = getattr(obj, "items", None)
            if hasattr(items, "all"):
                for it in items.all():
                    try:
                        total += float(getattr(it, "line_total")() if callable(getattr(it, "line_total", None)) else (it.unit_price or 0) * (it.quantity or 1))
                    except Exception:
                        pass
            return fmt_eur(total)
        return fmt_eur(val)
    total_price_col.short_description = "Total"

    def invoice_link(self, obj):
        inv = getattr(obj, "invoice", None)
        pdf = getattr(inv, "pdf", None)
        if inv and pdf and getattr(pdf, "url", None):
            try:
                url = pdf.url
                return format_html("<a href='{}' target='_blank'>PDF</a>", url)
            except Exception:
                return "-"
        return "-"
    invoice_link.short_description = "Invoice"

    # ---- readonly_fields helpers ----
    def subtotal_col(self, obj):      return fmt_eur(getattr(obj, "subtotal", 0))
    def discount_col(self, obj):      return fmt_eur(getattr(obj, "discount_amount", 0))
    def shipping_fee_col(self, obj):  return fmt_eur(getattr(obj, "shipping_fee", 0))
    def payment_ref_col(self, obj):   return getattr(obj, "payment_ref", "") or "-"
    def payment_id_col(self, obj):    return getattr(obj, "payment_id", "") or "-"

    def delivered_at(self, obj):      return getattr(obj, "delivered_at", None)
    def cancelled_at(self, obj):      return getattr(obj, "cancelled_at", None)

    def items_readonly(self, obj):
        rows = []
        items = getattr(obj, "items", None)
        if hasattr(items, "all"):
            for it in items.all():
                name = getattr(it, "product_name", None) or "-"
                unit_fmt = fmt_eur(getattr(it, "unit_price", None))
                try:
                    total_fmt = fmt_eur(it.line_total()) if callable(getattr(it, "line_total", None)) else fmt_eur((getattr(it, "unit_price", 0) or 0) * (getattr(it, "quantity", 1) or 1))
                except Exception:
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

    def shipping_snapshot_readonly(self, obj):
        fn = getattr(obj, "ship_first_name", "") or ""
        ln = getattr(obj, "ship_last_name", "") or ""
        line1 = getattr(obj, "ship_line1", "") or ""
        line2 = getattr(obj, "ship_line2", "") or ""
        postal = getattr(obj, "ship_postal", "") or ""
        city = getattr(obj, "ship_city", "") or ""
        country = getattr(obj, "ship_country", "") or ""
        phone = getattr(obj, "ship_phone", "") or ""
        street_name = getattr(obj, "ship_street_name", "") or ""
        house_number = getattr(obj, "ship_house_number", "") or ""
        return format_html(
            "<div>"
            "<div><b>{fn}</b> {ln}</div>"
            "<div>{line1}</div>"
            "<div>{line2}</div>"
            "<div>{postal} {city}</div>"
            "<div>{country}</div>"
            "<div>{phone}</div>"
            "<div>{street}</div>"
            "</div>",
            fn=fn, ln=ln, line1=line1, line2=line2, postal=postal, city=city,
            country=country, phone=phone, street=f"Street: {street_name} {house_number}".strip(),
        )
    shipping_snapshot_readonly.short_description = "Shipping snapshot"

# --------------------------------------------------------------------------------------
# Invoice
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
    list_display = ["email", "username,"] + ["is_active", "is_staff"]
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
    list_display = ("user", "full_name", "mobile_col", "verified_col", "language", "date_of_birth")
    search_fields = ("user__email", "full_name")
    list_select_related = ("user",)

    def mobile_col(self, obj):   return getattr(obj, "mobile", None) or "-"
    mobile_col.short_description = "Mobile"

    def verified_col(self, obj): return bool(getattr(obj, "verified", False))
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
    list_display = ["title_en", "category", "price_col", "quantity_col"]
    search_fields = ("title_en", "title_de", "category")

    def price_col(self, obj):
        val = getattr(obj, "price", None)
        if val is None:
            val = getattr(obj, "price_gross", None)
        return fmt_eur(val)
    price_col.short_description = "Price"

    def quantity_col(self, obj):
        q = getattr(obj, "quantity", None)
        return q if q is not None else "-"
    quantity_col.short_description = "Qty"

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
    list_select_related = ("user",)

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
    inlines = [ReturnAttachmentInline]

# --------------------------------------------------------------------------------------
# Misc simple models
# --------------------------------------------------------------------------------------
@admin.register(StockAlert)
class StockAlertAdmin(admin.ModelAdmin):
    list_display = ("product", "email", "is_active", "created_at", "notified_at")
    list_filter = ("is_active", "created_at", "notified_at")
    search_fields = ("email", "product__title_en", "product__title_de")
    list_select_related = ("product",)

@admin.register(OnDemandOrderRequest)
class OnDemandOrderRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "product_display", "quantity", "email", "created_at")
    list_filter = ("created_at",)
    search_fields = (
        "email", "name", "message",
        "product__title_en", "product__title_de", "product__key",
    )
    readonly_fields = ("created_at",)
    list_select_related = ("product",)

    def product_display(self, obj):
        title = getattr(obj.product, "title_en", None) or getattr(obj.product, "title_de", None) or str(obj.product)
        return f"{title} ({obj.product_id})"
    product_display.short_description = "Product"

# Keep the simple registers
admin.site.register(ContactMessage)
admin.site.register(NewsletterSubscriber)
admin.site.register(Review)
admin.site.register(StoneEnquiry)
admin.site.register(Wishlist)
admin.site.register(PromotionBanner)
admin.site.register(Coupon)
