# server/admin.py
from typing import Any, Callable, cast
from decimal import Decimal

from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.db import IntegrityError
from django.db.models.deletion import ProtectedError
from django.utils.html import format_html_join, format_html
from django.utils.translation import gettext_lazy as _
from django.db.models import Q,Count,F
from .utils.invoices import generate_invoice_pdf as _gen_inv_pdf
from django.utils import timezone
from django.urls import reverse
from .models import (
    Cart, CartItem, CouponRedemption, LoginOTP, Product, Wishlist, ProductMedia, NaturalStone, NaturalStoneImage, OnDemandOrderRequest,ProductEvent,
    StoneEnquiry, User, Profile, ContactMessage, NewsletterSubscriber, Coupon, StockAlert,
    Address, Order, Review, OrderItem, Invoice, ReturnRequest, ReturnAttachment, PromotionBanner
)
# --------------------------------------------------------------------------------------
# Optional utilities (safe fallbacks if utils are missing)
# --------------------------------------------------------------------------------------
# Always treat these as "fire-and-forget" in admin flows.
generate_invoice_pdf: Callable[..., None] = lambda *args, **kwargs: None
send_invoice_email:   Callable[..., None] = lambda *args, **kwargs: None
send_shipping_update: Callable[..., None] = lambda *args, **kwargs: None
send_cancellation_email: Callable[..., None] = lambda *args, **kwargs: None

try:
    from .utils.invoices import generate_invoice_pdf as _gen_inv_pdf
    # Under the hood it may return an Invoice/bytes/etc., but we treat as None here.
    generate_invoice_pdf = cast(Callable[..., None], _gen_inv_pdf)
except Exception:
    pass

try:
    from .utils.emails import (
        send_invoice_email as _send_invoice_email,
        send_shipping_update as _send_shipping_update,
        send_cancellation_email as _send_cancellation_email,
    )
    # Real functions might return bool; we don't use the return in admin actions.
    send_invoice_email     = cast(Callable[..., None], _send_invoice_email)
    send_shipping_update   = cast(Callable[..., None], _send_shipping_update)
    send_cancellation_email= cast(Callable[..., None], _send_cancellation_email)
except Exception:
    pass

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def to_decimal_or_zero(val: Any) -> Decimal:
    try:
        if isinstance(val, Decimal):
            return val
        if isinstance(val, (int, float, str)):
            return Decimal(str(val))
        # allow objects that stringify reasonably
        return Decimal(str(val))
    except Exception:
        return Decimal("0.00")
    
def shipping_fee(self, obj):
    try:
        return f"{to_decimal_or_zero(getattr(obj, 'shipping_price', 0)):.2f} €"
    except Exception:
        return "0.00 €"

def fmt_eur(v: Any) -> str:
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

    @admin.display(description="Item")
    def item_name(self, obj):
        name = getattr(obj, "product_name", None) or getattr(obj, "product_name_snapshot", None)
        if name:
            return name
        prod = getattr(obj, "product", None)
        if prod:
            return getattr(prod, "title_en", None) or getattr(prod, "title_de", None) or str(prod)
        return "-"

    @admin.display(description="Unit price")
    def unit_price_display(self, obj):
        unit = getattr(obj, "unit_price_gross", None)
        if unit is None:
            unit = getattr(obj, "unit_price", None)
        return fmt_eur(unit)

    @admin.display(description="Line total")
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


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    date_hierarchy = "created_at"
    inlines = [OrderItemInline]
    list_filter = ("status", "created_at")
    search_fields = ("number", "user__email", "email", "tracking_number", "coupon_redemption__coupon__code")
    actions = [mark_paid, mark_shipped, mark_cancelled]

    # --------------------------
    # Helpers used in admin UI
    # --------------------------
    @admin.display(description="Email")
    def customer_email(self, obj):
        return getattr(obj, "email", None) or getattr(getattr(obj, "user", None), "email", None) or "-"

    @admin.display(description="Total price")
    def total_price_display(self, obj):
        val = getattr(obj, "total_price", None)
        d = to_decimal_or_zero(val) if val is not None else None
        return f"{d:.2f} €" if d is not None else "-"

    @admin.display(description="Subtotal")
    def subtotal_readonly(self, obj):
        return f"{to_decimal_or_zero(getattr(obj, 'subtotal', 0)):.2f} €"

    @admin.display(description="Discount")
    def discount_readonly(self, obj):
        raw = getattr(obj, "discount_total", None)
        if raw is None:
            raw = getattr(obj, "discount_amount", 0)
        return f"-{to_decimal_or_zero(raw):.2f} €"

    @admin.display(description="Shipping fee")
    def shipping_fee_readonly(self, obj):
        raw = getattr(obj, "shipping_price", None)
        if raw is None:
            raw = getattr(obj, "shipping_fee", 0)
        return f"{to_decimal_or_zero(raw):.2f} €"

    @admin.display(description="VAT")
    def vat_readonly(self, obj):
        vat = getattr(obj, "vat_total", None)
        if vat is None:
            vat = getattr(obj, "vat_amount", None)
        if vat is None:
            vat = getattr(obj, "tax_amount", 0)
        return f"{to_decimal_or_zero(vat):.2f} €"

    @admin.display(description="Total price")
    def total_price_readonly(self, obj):
        return f"{to_decimal_or_zero(getattr(obj, 'total_price', 0)):.2f} €"

    @admin.display(description="Shipping snapshot")
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

    @admin.display(description="Items")
    def items_readonly(self, obj):
        items_mgr = getattr(obj, "items", None)
        if items_mgr is None or not hasattr(items_mgr, "all"):
            return "-"
        rows = []
        for it in items_mgr.all():
            name = getattr(it, "product_name", None) or getattr(it, "product_name_snapshot", None)
            if not name:
                prod = getattr(it, "product", None)
                name = (getattr(prod, "title_en", None) or getattr(prod, "title_de", None) or (str(prod) if prod else "-"))

            qty = getattr(it, "quantity", 1) or 1
            unit_raw = getattr(it, "unit_price_gross", None)
            if unit_raw is None:
                unit_raw = getattr(it, "unit_price", None)

            unit_dec = to_decimal_or_zero(unit_raw) if unit_raw is not None else None
            unit_fmt = f"{unit_dec:.2f} €" if unit_dec is not None else "-"

            try:
                if hasattr(it, "line_total") and callable(it.line_total):
                    total_dec = to_decimal_or_zero(it.line_total())
                else:
                    total_dec = to_decimal_or_zero(unit_raw or 0) * int(qty)
            except Exception:
                total_dec = None
            total_fmt = f"{total_dec:.2f} €" if total_dec is not None else "-"

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

    # ✅ Coupon tracking (admin)
    @admin.display(description="Coupon")
    def coupon_code_display(self, obj):
        try:
            red = getattr(obj, "coupon_redemption", None)
            coup = getattr(red, "coupon", None) if red else None
            return (getattr(coup, "code", "") or "").upper() or "-"
        except Exception:
            return "-"

    @admin.display(description="Coupon used by")
    def coupon_used_by_display(self, obj):
        try:
            red = getattr(obj, "coupon_redemption", None)
            if not red:
                return "-"
            u = getattr(red, "user", None)
            if u and getattr(u, "email", None):
                return u.email
            return getattr(obj, "email", None) or "-"
        except Exception:
            return "-"

    @admin.display(description="Coupon code")
    def coupon_code_readonly(self, obj):
        return self.coupon_code_display(obj)

    @admin.display(description="Coupon used by")
    def coupon_used_by_readonly(self, obj):
        return self.coupon_used_by_display(obj)

    # --------------------------
    # Safe dynamic config
    # --------------------------
    def _safe_fields(self, names):
        out = []
        for n in names:
            if hasattr(self, n) or hasattr(self.model, n):
                out.append(n)
        return tuple(out)

    # ✅ Django calls this for list page
    def get_list_display(self, request):
        return self._safe_fields([
            "number", "user", "customer_email", "status",
            "coupon_code_display", "coupon_used_by_display",
            "total_price_display", "created_at",
            "tracking_number", "tracking_carrier",
        ])

    # ✅ Django calls this for object edit page
    def get_readonly_fields(self, request, obj=None):
        base = [
            "number",
            "subtotal_readonly", "discount_readonly", "shipping_fee_readonly", "vat_readonly", "total_price_readonly",
            "payment_ref", "payment_id", "created_at", "updated_at",
            "items_readonly", "delivered_at", "cancelled_at",
            "customer_email", "shipping_snapshot_readonly",
            "coupon_code_readonly", "coupon_used_by_readonly",
        ]
        return self._safe_fields(base)

    def save_model(self, request, obj, form, change):
        prev = None
        if change and obj.pk:
            try:
                prev = Order.objects.get(pk=obj.pk)
            except Order.DoesNotExist:
                prev = None

        super().save_model(request, obj, form, change)

        should_send = False
        tracking = (getattr(obj, "tracking_number", "") or "").strip()

        try:
            if prev is None:
                if (getattr(obj, "status", "") == "shipped") or tracking:
                    should_send = True
            else:
                if getattr(prev, "status", "") != "shipped" and getattr(obj, "status", "") == "shipped":
                    should_send = True
                if (not getattr(prev, "shipped_at", None)) and getattr(obj, "shipped_at", None):
                    should_send = True
                if (not (getattr(prev, "tracking_number", "") or "").strip()) and tracking:
                    should_send = True
        except Exception:
            should_send = False

        if should_send:
            try:
                send_shipping_update(obj, tracking=tracking)
            except Exception:
                pass

# --------------------------------------------------------------------------------------
# Invoice
# --------------------------------------------------------------------------------------
@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("number", "order", "coupon_code", "created_at")  # ✅ add coupon_code
    search_fields = ("number", "order__number", "order__user__email", "order__coupon_redemption__coupon__code")
    list_select_related = ("order",)
    ordering = ("-created_at",)

    @admin.display(description="Coupon")
    def coupon_code(self, obj):
        try:
            o = getattr(obj, "order", None)
            red = getattr(o, "coupon_redemption", None) if o else None
            coup = getattr(red, "coupon", None) if red else None
            return (getattr(coup, "code", "") or "").upper() or "-"
        except Exception:
            return "-"

# --------------------------------------------------------------------------------------
# Cart
# --------------------------------------------------------------------------------------


class CartItemInline(admin.TabularInline):
    model = CartItem
    extra = 0
    can_delete = False

    # What columns to show in the inline:
    fields = (
        "product_admin",
        "product_title",
        "product_sku",
        "unit_price",
        "qty",
        "product_color",
        "product_dimensions",
        "product_category",
    )
    readonly_fields = fields

    @admin.display(description="Product")
    def product_admin(self, obj):
        if not obj.product_id:
            return "-"
        url = reverse("admin:server_product_change", args=[obj.product_id])
        return format_html('<a href="{}" target="_blank">Open</a>', url)

    @admin.display(description="Title")
    def product_title(self, obj):
        # adjust field names to your Product model
        return getattr(obj.product, "title_en", None) or getattr(obj.product, "title", "-")

    @admin.display(description="SKU / Key")
    def product_sku(self, obj):
        return getattr(obj.product, "key", "-")

    @admin.display(description="Unit price")
    def unit_price(self, obj):
        # if CartItem has its own stored price, prefer that:
        if hasattr(obj, "price") and obj.price is not None:
            return obj.price
        return getattr(obj.product, "price", "-")

    @admin.display(description="Color")
    def product_color(self, obj):
        return getattr(obj.product, "color", "-")

    @admin.display(description="Dimensions")
    def product_dimensions(self, obj):
        return getattr(obj.product, "dimensions", "-")

    @admin.display(description="Category")
    def product_category(self, obj):
        cat = getattr(obj.product, "category", None)
        return str(cat) if cat else "-"


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "cart_token", "is_active", "currency", "updated_at")
    list_filter = ("is_active", "currency")
    search_fields = ("cart_token", "user__email")
    inlines = [CartItemInline]


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
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "username", "password1", "password2", "is_staff", "is_active"),
            },
        ),
    )
    search_fields = ("email", "username")

    def _has_order_links(self, user):
        # Any order directly referencing this user, or (legacy) via shipping_address FK
        return Order.objects.filter(user=user).exists()

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
                super().delete_queryset(
                    request,
                    queryset.model.objects.filter(pk__in=[o.pk for o in ok]),
                )
            except (ProtectedError, IntegrityError) as e:
                self.message_user(
                    request,
                    _("Some deletions were blocked by related data. Details: %s") % (e,),
                    level=messages.ERROR,
                )
        if blocked:
            self.message_user(
                request,
                _(
                    "%d user(s) not deleted because orders reference them. "
                    "Deactivate instead."
                )
                % len(blocked),
                level=messages.WARNING,
            )

@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "full_name", "mobile_col", "verified_col", "language", "date_of_birth")
    search_fields = ("user__email", "full_name")
    list_select_related = ("user",)

    @admin.display(description="Mobile")
    def mobile_col(self, obj):   return getattr(obj, "mobile", None) or "-"
    

    @admin.display(boolean=True, description="Verified")
    def verified_col(self, obj): return bool(getattr(obj, "verified", False))
    
@admin.register(LoginOTP)
class LoginOTPAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "created_at", "expires_at", "attempts", "consumed_at", "ip")
    search_fields = ("user__email", "id")
    list_filter = ("consumed_at", "created_at", "expires_at")
    ordering = ("-created_at",)
    readonly_fields = ("id", "user", "created_at", "expires_at", "attempts", "consumed_at", "ip", "user_agent", "code_hash")
# --------------------------------------------------------------------------------------
# Product & Media
# --------------------------------------------------------------------------------------
@admin.register(ProductEvent)
class ProductEventAdmin(admin.ModelAdmin):
    list_display = ("id", "product", "action", "user", "created_at")
    list_filter = ("action", "created_at")
    search_fields = ("product__title_en", "product__title_de", "user__email")
    date_hierarchy = "created_at"
    autocomplete_fields = ("product", "user")

class ProductMediaInline(admin.TabularInline):
    model = ProductMedia
    extra = 1
    fields = ["image", "video", "caption", "order", "media_type"]

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    readonly_fields = ("slug",)
    inlines = [ProductMediaInline]
    list_display = ["title_en", "category", "price_col", "quantity_col", "demand_score_display", "add_to_carts_display", "wishlists_display","views_display",]
    search_fields = ("title_en", "title_de", "category")
    list_filter = ()
    ordering = ("-id",)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # Add per-product counts
        qs = qs.annotate(
            views=Count("events", filter=Q(events__action="view"), distinct=False),
            wishlists=Count("events", filter=Q(events__action="wishlist"), distinct=False),
            add_to_carts=Count("events", filter=Q(events__action="add_to_cart"), distinct=False),
        ).annotate(
            demand_score=F("add_to_carts") * 3 + F("wishlists") * 2 + F("views")
        ).order_by("-demand_score", "-id")
        return qs

    @admin.display(ordering="demand_score", description="Demand score")
    def demand_score_display(self, obj):
        return int(getattr(obj, "demand_score", 0) or 0)

    @admin.display(ordering="add_to_carts", description="Adds to cart")
    def add_to_carts_display(self, obj):
        return int(getattr(obj, "add_to_carts", 0) or 0)

    @admin.display(ordering="wishlists", description="Wishlists")
    def wishlists_display(self, obj):
        return int(getattr(obj, "wishlists", 0) or 0)

    @admin.display(ordering="views", description="Views")
    def views_display(self, obj):
        return int(getattr(obj, "views", 0) or 0)

    @admin.display(description="Price")
    def price_col(self, obj):
        # prefer gross if present
        val = getattr(obj, "price_gross", None)
        if val is None:
            val = getattr(obj, "price", None)
        return fmt_eur(val)

    @admin.display(description="Quantity")
    def quantity_col(self, obj):
        q = getattr(obj, "quantity", None)
        return q if q is not None else "-"
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

    _possible_search = (
        "user__email", "first_name", "last_name", "full_name",
        "address_line1", "street_name", "postal_code", "city",
    )
    search_fields = tuple(f for f in _possible_search if hasattr(Address, f.split("__")[0]))
    list_select_related = ("user",)

    def delete_model(self, request, obj):
        try:
            super().delete_model(request, obj)
        except (ProtectedError, IntegrityError):
            self.message_user(
                request,
                _("Cannot delete this address because one or more orders reference it."),
                level=messages.ERROR,
            )

    def delete_queryset(self, request, queryset):
        ok_ids = []
        blocked = 0
        for addr in queryset:
            try:
                super(AddressAdmin, self).delete_model(request, addr)
                ok_ids.append(addr.pk)
            except (ProtectedError, IntegrityError):
                blocked += 1
        if blocked:
            self.message_user(
                request,
                _("%d address(es) not deleted because orders reference them.") % blocked,
                level=messages.WARNING,
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

    @admin.display(description="Product")
    def product_display(self, obj):
        title = getattr(obj.product, "title_en", None) or getattr(obj.product, "title_de", None) or str(obj.product)
        return f"{title} ({obj.product_id})"
# --------------------------------------------------------------------------------------
# Coupon
# --------------------------------------------------------------------------------------
class CouponRedemptionInline(admin.TabularInline):
    model = CouponRedemption
    extra = 0
    can_delete = False
    fields = ("order_link", "order_status", "user_email", "created_at")
    readonly_fields = ("order_link", "order_status", "user_email", "created_at")

    @admin.display(description="Order")
    def order_link(self, obj):
        try:
            o = getattr(obj, "order", None)
            if not o:
                return "-"
            # admin change link
            return format_html(
                "<a href='/admin/server/order/{}/change/'>#{}</a>",
                o.pk,
                getattr(o, "number", o.pk),
            )
        except Exception:
            return "-"

    @admin.display(description="Order status")
    def order_status(self, obj):
        try:
            o = getattr(obj, "order", None)
            return getattr(o, "status", "-") if o else "-"
        except Exception:
            return "-"

    @admin.display(description="Used by")
    def user_email(self, obj):
        try:
            u = getattr(obj, "user", None)
            if u and getattr(u, "email", None):
                return u.email
            # guest: fall back to order.email
            o = getattr(obj, "order", None)
            return getattr(o, "email", "-") if o else "-"
        except Exception:
            return "-"


@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    list_display = (
        "code", "kind", "value", "active", "starts_at", "ends_at", "is_birthday",
        "paid_uses", "paid_unique_users"  # ✅ NEW counters
    )
    list_filter  = ("active", "is_birthday", "starts_at", "ends_at", "kind")
    search_fields = ("code",)

    # ✅ show who used it (paid orders only)
    inlines = [CouponRedemptionInline]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # annotate paid usage counts
        return qs.annotate(
            _paid_uses=Count("redemptions", filter=Q(redemptions__order__status="paid"), distinct=True),
            _paid_users=Count("redemptions__user", filter=Q(redemptions__order__status="paid"), distinct=True),
        )

    @admin.display(description="Paid uses")
    def paid_uses(self, obj):
        return getattr(obj, "_paid_uses", 0)

    @admin.display(description="Paid users")
    def paid_unique_users(self, obj):
        return getattr(obj, "_paid_users", 0)


# --------------------------------------------------------------------------------------
# Promotion banner
# --------------------------------------------------------------------------------------
@admin.register(PromotionBanner)
class PromotionBannerAdmin(admin.ModelAdmin):
    list_display = tuple([f for f in ["id", "name", "headline", "is_enabled", "start_at", "end_at", "priority"] if hasattr(PromotionBanner, f)])
    list_filter  = tuple([f for f in ["is_enabled", "start_at", "end_at"] if hasattr(PromotionBanner, f)])
    search_fields = tuple([f for f in ["name", "headline", "subheadline"] if hasattr(PromotionBanner, f)])

    base_fields = [
        "name", "coupon", "code",
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
