# server/admin.py
from django.contrib import admin
from django.utils.html import format_html_join, format_html
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import (
    Product, Wishlist, ProductMedia, NaturalStone, NaturalStoneImage,
    StoneEnquiry, User, Profile, ContactMessage, NewsletterSubscriber,
    Coupon, StockAlert, Address, Order, Review, OrderItem, Invoice,
    ReturnRequest, ReturnAttachment, Promotion
)
from .utils.invoices import generate_invoice_pdf
from .utils.emails import (
    send_invoice_email, send_shipping_update, send_cancellation_email
)
from .utils.background import fire_and_forget
from .tasks import post_payment_pipeline

# ----- actions -----
@admin.action(description="Mark selected orders as PAID (+ send invoice)")
def mark_paid(modeladmin, request, queryset):
    n = 0
    for order in queryset:
        if order.status != "paid":
            order.status = "paid"
            order.save(update_fields=["status"])
            fire_and_forget(post_payment_pipeline, order.id)
            # ensure invoice then email (idempotent)
            generate_invoice_pdf(order)
            send_invoice_email(order)
            n += 1
    modeladmin.message_user(request, f"Marked {n} order(s) as paid and sent invoice.")

@admin.action(description="Mark selected orders as SHIPPED (+ email)")
def mark_shipped(modeladmin, request, queryset):
    n = queryset.update(status="shipped")
    for order in queryset:
        send_shipping_update(order, tracking=getattr(order, "tracking_number", ""))
    modeladmin.message_user(request, f"Marked {n} order(s) as shipped and sent shipping email.")

@admin.action(description="Cancel selected orders (+ notify customer)")
def mark_cancelled(modeladmin, request, queryset):
    n = queryset.update(status="cancelled")
    for order in queryset:
        send_cancellation_email(order)
    modeladmin.message_user(request, f"Cancelled {n} order(s) and notified customers.")

@admin.action(description="Assign numbers to selected orders (if missing)")
def assign_numbers(modeladmin, request, queryset):
    updated = 0
    for o in queryset:
        if not getattr(o, "number", None):
            if hasattr(o, "assign_unique_order_number"):
                o.assign_unique_order_number()
            else:
                o.number = f"MM{o.pk:08d}"
            o.save(update_fields=["number"])
            updated += 1
    modeladmin.message_user(request, f"Assigned numbers to {updated} order(s).")

# ---------- User & Profile ----------
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
    list_display = ["user", "full_name", "mobile", "verified"]
    search_fields = ["user__email", "full_name", "mobile"]

# ---------- Invoice ----------
@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("number", "order", "created_at")
    search_fields = ("number", "order__number", "order__user__email")

# ---------- Coupon / Promotion ----------
@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    list_display = ("code", "kind", "value", "active", "starts_at", "ends_at", "min_subtotal")
    list_filter  = ("active", "kind")
    search_fields = ("code",)
    filter_horizontal = ("products",)

@admin.register(Promotion)
class PromotionAdmin(admin.ModelAdmin):
    list_display = ("name", "code_display", "start_at", "end_at", "is_enabled", "priority")
    list_filter = ("is_enabled",)
    search_fields = ("name", "code", "coupon__code")
    readonly_fields = ("created_at", "updated_at")

    def code_display(self, obj):
        return obj.current_code()
    code_display.short_description = "Coupon code"

# ---------- Order items inline ----------
class OrderItemInline(admin.TabularInline):
    model = OrderItem
    fk_name = "order"
    extra = 0
    can_delete = False
    readonly_fields = ("item_name", "quantity", "unit_price_fmt", "line_total_fmt")
    fields = ("item_name", "quantity", "unit_price_fmt", "line_total_fmt")
    verbose_name = "Order item"
    verbose_name_plural = "Order items"

    def has_add_permission(self, request, obj=None):
        return False

    def item_name(self, obj):
        for attr in ("product_name", "name",):
            val = getattr(obj, attr, None)
            if val:
                return val
        # Fallback: use product.__str__ if present
        prod = getattr(obj, "product", None)
        return str(prod) if prod else "-"
    item_name.short_description = "Item"

    def unit_price_fmt(self, obj):
        val = getattr(obj, "unit_price", None)
        try:
            return f"{val:.2f} €" if val is not None else "-"
        except Exception:
            return str(val) if val is not None else "-"
    unit_price_fmt.short_description = "Unit price"

    def line_total_fmt(self, obj):
        if hasattr(obj, "line_total") and callable(getattr(obj, "line_total")):
            try:
                return f"{obj.line_total():.2f} €"
            except Exception:
                pass
        # Fallback compute
        qty = getattr(obj, "quantity", 0) or 0
        unit = getattr(obj, "unit_price", 0) or 0
        try:
            return f"{(qty * unit):.2f} €"
        except Exception:
            return "-"
    line_total_fmt.short_description = "Line total"

# ---------- Order ----------
@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    date_hierarchy = "created_at"
    list_display = ("number", "user", "email", "status", "total_price", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("number", "user__email", "email")
    actions = [mark_paid, mark_shipped, mark_cancelled]
    readonly_fields = (
        "number", "subtotal", "discount_amount", "shipping_fee", "total_price",
        "payment_ref", "payment_id", "created_at", "updated_at",
        "items_readonly", "delivered_at", "cancelled_at",
    )
    fields = (
        ("number", "user", "email",  "created_at", "delivered_at", "cancelled_at"),
        ("status", "payment_method", "tracking_number"),
        ("subtotal", "discount_amount", "shipping_fee", "total_price"),
        ("shipping_address",),
        "items_readonly",
        ("payment_ref", "payment_id"),
    )
    inlines = [OrderItemInline]

    def customer_email(self, obj):
        # Prefer explicit order email if your model supports guest checkout
        val = getattr(obj, "email", None) or getattr(getattr(obj, "user", None), "email", None)
        return val or "-"
    customer_email.short_description = "Email"

    def total_price_display(self, obj):
        val = getattr(obj, "total_price", None)
        if val is None:
            # Try common alternates
            for alt in ("total", "grand_total", "amount_total"):
                v = getattr(obj, alt, None)
                if v is not None:
                    val = v
                    break
        try:
            return f"{val:.2f} €" if val is not None else "-"
        except Exception:
            return str(val) if val is not None else "-"
    total_price_display.short_description = "Total"

    # ---- readonly_fields helpers (names here match readonly_fields) ----
    def subtotal(self, obj):
        if hasattr(obj, "subtotal"):
            return obj.subtotal
        # Fallback: sum items
        rel = getattr(obj, "items", None) or getattr(obj, "orderitem_set", None)
        total = 0
        if rel is not None:
            for it in rel.all():
                qty = getattr(it, "quantity", 0) or 0
                price = getattr(it, "unit_price", 0) or 0
                total += qty * price
        return f"{total:.2f} €"

    def discount_amount(self, obj):
        for name in ("discount_amount", "discount", "coupon_discount"):
            val = getattr(obj, name, None)
            if val is not None:
                try:
                    return f"{val:.2f} €"
                except Exception:
                    return str(val)
        return "0.00 €"

    def shipping_fee(self, obj):
        for name in ("shipping_fee", "shipping_cost", "shipping_price", "delivery_fee"):
            val = getattr(obj, name, None)
            if val is not None:
                try:
                    return f"{val:.2f} €"
                except Exception:
                    return str(val)
        return "0.00 €"

    def total_price(self, obj):
        val = getattr(obj, "total_price", None)
        if val is None:
            # Compute from subtotals as a fallback
            try:
                # reuse helpers
                sub = self.subtotal(obj)
                ship = self.shipping_fee(obj)
                disc = self.discount_amount(obj)
                # strip " €" if present
                def to_dec(s):
                    return float(str(s).replace(" €", ""))
                val = to_dec(sub) + to_dec(ship) - to_dec(disc)
            except Exception:
                return "-"
        try:
            return f"{val:.2f} €"
        except Exception:
            return str(val)

    def payment_ref(self, obj):
        # Safe getter for a reference string
        for name in ("payment_ref", "payment_reference", "provider_ref"):
            val = getattr(obj, name, None)
            if val:
                return val
        return "-"

    def payment_id(self, obj):
        for name in ("payment_id", "provider_payment_id", "transaction_id"):
            val = getattr(obj, name, None)
            if val:
                return val
        return "-"

    def delivered_at(self, obj):
        for name in ("delivered_at", "delivered_on", "shipped_at"):
            val = getattr(obj, name, None)
            if val:
                return val
        return None

    def cancelled_at(self, obj):
        for name in ("cancelled_at", "canceled_at", "voided_at"):
            val = getattr(obj, name, None)
            if val:
                return val
        return None
    
    def items_readonly(self, obj):
        rel = getattr(obj, "items", None) or getattr(obj, "orderitem_set", None)
        if rel is None:
            return "-"
        rows = []
        for it in rel.all():
            # item name
            name = getattr(it, "product_name", None)
            if not name:
                prod = getattr(it, "product", None)
                name = str(prod) if prod else getattr(it, "name", "-")
            # unit price
            unit = getattr(it, "unit_price", 0) or 0
            try:
                unit_fmt = f"{unit:.2f} €"
            except Exception:
                unit_fmt = str(unit)
            # line total
            if hasattr(it, "line_total") and callable(getattr(it, "line_total")):
                try:
                    total_fmt = f"{it.line_total():.2f} €"
                except Exception:
                    total_fmt = "-"
            else:
                qty = getattr(it, "quantity", 0) or 0
                try:
                    total_fmt = f"{(qty * unit):.2f} €"
                except Exception:
                    total_fmt = "-"
            rows.append((f"{name} × {getattr(it,'quantity',0)}", unit_fmt, total_fmt))

        if not rows:
            return "-"
        return format_html(
            "<table style='width:100%'><tr><th align='left'>Item</th>"
            "<th align='right'>Unit</th><th align='right'>Total</th></tr>{}</table>",
            format_html_join("", "<tr><td>{}</td><td align='right'>{}</td><td align='right'>{}</td></tr>", rows)
        )
    items_readonly.short_description = "Items"

# ---------- OrderItem ----------
@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ("order", "product", "quantity", "unit_price")
    search_fields = ("order__number", "product__title_en", "product__title_de")

# ---------- ReturnRequest ----------
class ReturnAttachmentInline(admin.TabularInline):
    model = ReturnAttachment
    extra = 0

@admin.register(ReturnRequest)
class ReturnRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "user", "request_type", "status", "created_at")
    list_filter  = ("request_type", "status", "created_at")
    search_fields = ("order__number", "order__id", "user__email")
    date_hierarchy = "created_at"
    inlines = [ReturnAttachmentInline]

# ---------- Product & Media ----------
class NaturalStoneImageInline(admin.TabularInline):
    model = NaturalStoneImage
    extra = 1
    max_num = 10
    fields = ["image"]
    verbose_name = "Additional Image"
    verbose_name_plural = "Upload up to 10 images"

class ProductMediaInline(admin.TabularInline):
    model = ProductMedia
    extra = 1
    fields = ["image", "video", "caption", "order", "media_type"]

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    inlines = [ProductMediaInline]

    list_display = [
        "title_display",
        "category_display",
        "price_display",   # was 'price'
        "stock_display",   # was 'stock' or 'quantity'
        "is_active_display",
    ]
    search_fields = ["title_en", "title_de"]
    list_filter = ["category"]

    # ---- Display helpers ----
    def title_display(self, obj):
        return getattr(obj, "title_en", None) or getattr(obj, "title", None) or str(obj)
    title_display.short_description = "Title"

    def category_display(self, obj):
        # Adjust if you use FK with __str__ or a CharField
        return getattr(obj, "category", "") or "-"
    category_display.short_description = "Category"

    def price_display(self, obj):
        """
        Try common price field names; fall back to a @property or method if present.
        """
        for attr in ("price", "price_gross", "price_net", "unit_price", "base_price"):
            val = getattr(obj, attr, None)
            if val is not None:
                try:
                    return f"{val:.2f} €"
                except Exception:
                    return str(val)
        # Try a callable like vat_included_price()
        vip = getattr(Product, "vat_included_price", None)
        if callable(vip):
            try:
                val = vip(obj)
                return f"{val:.2f} €"
            except Exception:
                pass
        return "-"
    price_display.short_description = "Price"

    def stock_display(self, obj):
        """
        Try common inventory field names.
        """
        for attr in ("quantity", "stock", "inventory", "stock_count", "qty"):
            val = getattr(obj, attr, None)
            if val is not None:
                return val
        return 0
    stock_display.short_description = "Stock"

    def is_active_display(self, obj):
        """
        If you have 'is_active' or 'active' or publish flags, show one.
        """
        for attr in ("is_active", "active", "published", "is_published"):
            val = getattr(obj, attr, None)
            if isinstance(val, bool):
                return val
        return True
    is_active_display.boolean = True
    is_active_display.short_description = "Active"

@admin.register(NaturalStone)
class NaturalStoneAdmin(admin.ModelAdmin):
    list_display = ["type"]
    inlines = [NaturalStoneImageInline]

@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    list_display = (
        "user", "first_name", "last_name", "address_line1",
        "postal_code", "city", "country", "phone", "is_default", "created_at",
    )
    list_filter = ("country", "is_default", "created_at")
    search_fields = (
        "user__email", "first_name", "last_name", "full_name",
        "address_line1", "postal_code", "city",
    )
    actions = ["make_default"]

    def make_default(self, request, queryset):
        for addr in queryset:
            addr.set_default_for_user()
        self.message_user(request, f"Set {queryset.count()} address(es) as default.")

@admin.register(StockAlert)
class StockAlertAdmin(admin.ModelAdmin):
    list_display = ("product", "email", "is_active", "created_at", "notified_at")
    list_filter = ("is_active", "created_at", "notified_at")
    search_fields = ("email", "product__title_en", "product__title_de")

# simple registrations
admin.site.register(NaturalStoneImage)
admin.site.register(ContactMessage)
admin.site.register(NewsletterSubscriber)
admin.site.register(Review)
admin.site.register(StoneEnquiry)
admin.site.register(Wishlist)
