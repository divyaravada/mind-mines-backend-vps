# server/admin.py
from typing import Any, Optional
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import ReadOnlyPasswordHashField
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models.deletion import ProtectedError
from django.utils.html import format_html_join, format_html
from django.utils.translation import gettext_lazy as _
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

def _reserve_stock(order: Order):
    fn = getattr(order, "reserve_stock_atomic", None) or getattr(order, "reserve_stock", None)
    if callable(fn):
        try:
            fn()
        except Exception:
            pass

def _release_stock(order: Order):
    fn = getattr(order, "release_stock_atomic", None) or getattr(order, "release_stock", None)
    if callable(fn):
        try:
            fn()
        except Exception:
            pass

# ----- admin actions -----
@admin.action(description="Mark selected orders as PAID (+ generate/send invoice)")
def mark_paid(modeladmin, request, queryset):
    updated = 0
    for order in queryset.select_related("user"):
        if getattr(order, "status", None) == "paid":
            continue
        order.status = "paid"
        setattr(order, "payment_status", "paid")
        # prefer setting paid_at if model has it
        if hasattr(order, "paid_at"):
            from django.utils import timezone
            order.paid_at = timezone.now()
            order.save(update_fields=["status", "payment_status", "paid_at"])
        else:
            order.save(update_fields=["status", "payment_status"])

        _reserve_stock(order)

        # invoice + email
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
        request, f"Marked {updated} order(s) as paid, reserved stock, and attempted to send invoice.",
        level=messages.SUCCESS
    )

@admin.action(description="Mark selected orders as SHIPPED (+ email)")
def mark_shipped(modeladmin, request, queryset):
    from django.utils import timezone
    n = 0
    for order in queryset:
        order.status = "shipped"
        if hasattr(order, "shipped_at") and not order.shipped_at:
            order.shipped_at = timezone.now()
            order.save(update_fields=["status", "shipped_at"])
        else:
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
        if hasattr(order, "payment_status"):
            setattr(order, "payment_status", "cancelled")
            order.save(update_fields=["status", "payment_status"])
        else:
            order.save(update_fields=["status"])
        if prev != "cancelled":
            _release_stock(order)
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
        # prefer stored snapshot
        name = getattr(obj, "product_name", None) or getattr(obj, "product_name_snapshot", None)
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
    list_display = (
        "number", "user", "customer_email", "status",
        "total_price_display", "created_at",
        "tracking_number", "tracking_carrier",
    )
    list_filter = ("status", "created_at")
    search_fields = ("number", "user__email", "email", "tracking_number")
    actions = [mark_paid, mark_shipped, mark_cancelled]

    readonly_fields = (
        "number",
        "subtotal_readonly", "discount_readonly", "shipping_fee_readonly", "vat_readonly", "total_price_readonly",
        "payment_ref", "payment_id", "created_at", "updated_at",
        "items_readonly", "delivered_at", "cancelled_at",
        "customer_email", "shipping_snapshot_readonly",
    )

    fieldsets = (
        (None, {"fields": ("number","user","customer_email","created_at","delivered_at","cancelled_at")}),
        ("Status & Payment", {"fields": ("status","payment_method","payment_ref","payment_id")}),
        ("Tracking", {"fields": ("tracking_number","tracking_carrier","tracking_url","shipped_at")}),
        ("Totals (read-only)", {"fields": ("subtotal_readonly","discount_readonly","shipping_fee_readonly","vat_readonly","total_price_readonly")}),
        ("Shipping snapshot", {"fields": ("shipping_snapshot_readonly",)}),
        ("Items", {"fields": ("items_readonly",)}),
    )

    inlines = [OrderItemInline]

    # ---- list_display helpers ----
    def customer_email(self, obj):
        return getattr(obj, "email", None) or getattr(getattr(obj, "user", None), "email", None) or "-"
    customer_email.short_description = "Email"

    def total_price_display(self, obj):
        val = getattr(obj, "total_price", None)
        try:
            return f"{Decimal(val):.2f} €" if val is not None else "-"
        except (InvalidOperation, TypeError, ValueError):
            return "-"

    # ---- totals (readonly) ----
    def subtotal_readonly(self, obj):
        try: return f"{Decimal(getattr(obj, 'subtotal', 0) or 0):.2f} €"
        except (InvalidOperation, TypeError, ValueError): return "0.00 €"
    subtotal_readonly.short_description = "Subtotal"

    def discount_readonly(self, obj):
        try: return f"-{Decimal(getattr(obj, 'discount_amount', 0) or 0):.2f} €"
        except (InvalidOperation, TypeError, ValueError): return "0.00 €"
    discount_readonly.short_description = "Discount"

    def shipping_fee_readonly(self, obj):
        try: return f"{Decimal(getattr(obj, 'shipping_fee', 0) or 0):.2f} €"
        except (InvalidOperation, TypeError, ValueError): return "0.00 €"
    shipping_fee_readonly.short_description = "Shipping"

    def vat_readonly(self, obj):
        vat = getattr(obj, "vat_amount", None)
        if vat is None:
            vat = getattr(obj, "tax_amount", 0)
        try: return f"{Decimal(vat or 0):.2f} €"
        except (InvalidOperation, TypeError, ValueError): return "0.00 €"
    vat_readonly.short_description = "VAT"

    def total_price_readonly(self, obj):
        try: return f"{Decimal(getattr(obj, 'total_price', 0) or 0):.2f} €"
        except (InvalidOperation, TypeError, ValueError): return "0.00 €"
    total_price_readonly.short_description = "Grand total"

    # ---- payment/refs read-only helpers (exist as model fields) ----
    def payment_ref(self, obj): return getattr(obj, "payment_ref", "") or "-"
    def payment_id(self, obj):  return getattr(obj, "payment_id", "") or "-"

    def delivered_at(self, obj):  return getattr(obj, "delivered_at", None)
    def cancelled_at(self, obj):  return getattr(obj, "cancelled_at", None)

    # ---- pretty shipping snapshot ----
    def shipping_snapshot_readonly(self, obj):
        fn  = (getattr(obj, "ship_first_name", "") or "").strip()
        ln  = (getattr(obj, "ship_last_name", "") or "").strip()
        l1  = (getattr(obj, "ship_line1", "") or "").strip()
        l2  = (getattr(obj, "ship_line2", "") or "").strip()
        pc  = (getattr(obj, "ship_postal", "") or "").strip()
        city = (getattr(obj, "ship_city", "") or "").strip()
        cty  = (getattr(obj, "ship_country", "") or "").strip()
        ph   = (getattr(obj, "ship_phone", "") or "").strip()
        stn  = (getattr(obj, "ship_street_name", "") or "").strip()
        hno  = (getattr(obj, "ship_house_number", "") or "").strip()
        # detect if snapshot is basically empty
        snapshot_missing = not any([fn, ln, l1, l2, pc, city, cty, ph, stn, hno])

        # fallback to related Address (legacy)
        if snapshot_missing and hasattr(obj, "shipping_address"):
            addr = getattr(obj, "shipping_address", None)
            if addr:
                fn  = (getattr(addr, "first_name", "") or "").strip()
                ln  = (getattr(addr, "last_name", "") or "").strip()
                l1  = (getattr(addr, "address_line1", "") or "").strip()
                l2  = (getattr(addr, "apartment", "") or "").strip()
                pc  = (getattr(addr, "postal_code", "") or "").strip()
                city = (getattr(addr, "city", "") or "").strip()
                cty  = (getattr(addr, "country", "") or "").strip()
                ph   = (getattr(addr, "phone", "") or "").strip()
                stn  = (getattr(addr, "street_name", "") or "").strip()
                hno  = (getattr(addr, "house_number", "") or "").strip()

        if not any([fn, ln, l1, l2, pc, city, cty, ph, stn, hno]):
            return "-"

        street_line = " ".join(x for x in [stn, hno] if x).strip()
        return format_html(
            "<div>"
            "<div><b>{fn}</b> {ln}</div>"
            "<div>{l1}</div>"
            "<div>{l2}</div>"
            "<div>{pc} {city}</div>"
            "<div>{cty}</div>"
            "<div>{ph}</div>"
            "{street}"
            "</div>",
            fn=fn, ln=ln, l1=l1, l2=l2, pc=pc, city=city, cty=cty, ph=ph,
            street=format_html("<div>Street: {}</div>", street_line) if street_line else "",
        )
    shipping_snapshot_readonly.short_description = "Shipping snapshot"

    # ---- items pretty table ----
    def items_readonly(self, obj):
        items = getattr(obj, "items", None)
        if not hasattr(items, "all"):
            return "-"
        rows = []
        for it in items.all():
            name = getattr(it, "product_name", None) or getattr(it, "product_name_snapshot", None)
            if not name:
                prod = getattr(it, "product", None)
                name = (getattr(prod, "title_en", None) or
                        getattr(prod, "title_de", None) or
                        (str(prod) if prod else "-"))
            qty = getattr(it, "quantity", 1) or 1
            unit = getattr(it, "unit_price_gross", None)
            if unit is None:
                unit = getattr(it, "unit_price", None)
            try:
                unit_fmt = f"{Decimal(unit):.2f} €" if unit is not None else "-"
            except (InvalidOperation, TypeError, ValueError):
                unit_fmt = "-"
            # line total
            total_val = None
            try:
                if hasattr(it, "line_total") and callable(it.line_total):
                    total_val = it.line_total()
                else:
                    total_val = (Decimal(unit or 0) * qty)
            except Exception:
                total_val = None
            try:
                total_fmt = f"{Decimal(total_val):.2f} €" if total_val is not None else "-"
            except (InvalidOperation, TypeError, ValueError):
                total_fmt = "-"
            rows.append((f"{name} × {qty}", unit_fmt, total_fmt))

        if not rows:
            return "-"
        return format_html(
            "<table style='width:100%'><tr><th align='left'>Item</th>"
            "<th align='right'>Unit</th><th align='right'>Total</th></tr>{}</table>",
            format_html_join(
                "",
                "<tr><td>{}</td><td align='right'>{}</td><td align='right'>{}</td></tr>",
                rows
            )
        )
    items_readonly.short_description = "Items"

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
    list_display = ["email", "username", "is_active", "is_staff"]
    ordering = ["email"]
    fieldsets = (
        (None, {"fields": ("email", "password", "username")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "username", "password1", "password2", "is_staff", "is_active")}),
    )
    search_fields = ("email", "username")

    def _has_order_links(self, user):
        # Any order directly referencing this user, or (legacy) via shipping_address FK
        if Order.objects.filter(user=user).exists():
            return True
        if hasattr(Order, "shipping_address"):
            from django.db.models import Q
            return Order.objects.filter(Q(shipping_address__user=user)).exists()
        return False

    def delete_model(self, request, obj):
        if self._has_order_links(obj):
            self.message_user(
                request,
                _("Cannot delete this user because related orders reference the user or their addresses. "
                  "Deactivate the user instead."),
                level=messages.ERROR,
            )
            return
        try:
            super().delete_model(request, obj)
        except (ProtectedError, IntegrityError) as e:
            self.message_user(
                request,
                _("Deletion blocked by related data (orders/addresses). Details: %s") % (e,),
                level=messages.ERROR,
            )

    def delete_queryset(self, request, queryset):
        blocked, ok = [], []
        for obj in queryset:
            (blocked if self._has_order_links(obj) else ok).append(obj)
        if ok:
            try:
                super().delete_queryset(request, queryset.model.objects.filter(pk__in=[o.pk for o in ok]))
            except (ProtectedError, IntegrityError) as e:
                self.message_user(
                    request,
                    _("Some deletions were blocked by related data. Details: %s") % (e,),
                    level=messages.ERROR,
                )
        if blocked:
            self.message_user(
                request,
                _("%d user(s) not deleted because orders reference them or their addresses. "
                  "Deactivate instead.") % len(blocked),
                level=messages.WARNING,
            )

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
        # prefer gross if present
        val = getattr(obj, "price_gross", None)
        if val is None:
            val = getattr(obj, "price", None)
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
# Stock alert & On-demand
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

# --------------------------------------------------------------------------------------
# Coupon
# --------------------------------------------------------------------------------------
@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    """
    Support the new Coupon fields used in views:
    - code, kind, value, active, starts_at, ends_at, is_birthday
    Optionally show usage_limit/times_used if present.
    """
    base_fields = ["code", "kind", "value", "active", "starts_at", "ends_at", "is_birthday", "usage_limit", "times_used"]
    def _has(model, f): return hasattr(model, f)

    fields = [f for f in base_fields if hasattr(Coupon, f)]
    list_display = tuple([f for f in ["code", "kind", "value", "active", "starts_at", "ends_at", "is_birthday", "times_used"] if hasattr(Coupon, f)])
    list_filter  = tuple([f for f in ["active", "is_birthday", "starts_at", "ends_at", "kind"] if hasattr(Coupon, f)])
    search_fields = tuple([f for f in ["code"] if hasattr(Coupon, f)])

    def get_readonly_fields(self, request, obj=None):
        ro = []
        if hasattr(Coupon, "times_used"):
            ro.append("times_used")
        return ro

# --------------------------------------------------------------------------------------
# Promotion banner
# --------------------------------------------------------------------------------------
@admin.register(PromotionBanner)
class PromotionBannerAdmin(admin.ModelAdmin):
    list_display = tuple([f for f in ["id", "headline", "is_enabled", "start_at", "end_at", "priority"] if hasattr(PromotionBanner, f)])
    list_filter  = tuple([f for f in ["is_enabled", "start_at", "end_at"] if hasattr(PromotionBanner, f)])
    search_fields = tuple([f for f in ["headline", "subheadline"] if hasattr(PromotionBanner, f)])

    base_fields = [
        "headline", "subheadline", "is_enabled", "priority",
        "start_at", "end_at", "bg_color", "text_color",
        "link_url", "country_whitelist", "image"
    ]
    fields = [f for f in base_fields if hasattr(PromotionBanner, f)]
    readonly_fields = []

# --------------------------------------------------------------------------------------
# Simple registers
# --------------------------------------------------------------------------------------
admin.site.register(ContactMessage)
admin.site.register(NewsletterSubscriber)
admin.site.register(Review)
admin.site.register(StoneEnquiry)
admin.site.register(Wishlist)
