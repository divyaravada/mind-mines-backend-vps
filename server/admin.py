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


# ----- bulk actions -----
def mark_paid(self, request, queryset):
    n = 0
    for order in queryset:
        if order.status != "paid":
            order.status = "paid"
            order.save(update_fields=["status"])
            fire_and_forget(post_payment_pipeline, order.id)
            if not hasattr(order, "invoice") or not order.invoice.pdf:
                generate_invoice_pdf(order)
            send_invoice_email(order)
            n += 1
    self.message_user(request, f"Marked {n} order(s) as paid and sent invoice.")
mark_paid.short_description = "Mark selected orders as PAID (+ send invoice)"

def mark_shipped(self, request, queryset):
    n = queryset.update(status="shipped")
    for order in queryset:
        send_shipping_update(order)
    self.message_user(request, f"Marked {n} order(s) as shipped and sent shipping email.")
mark_shipped.short_description = "Mark selected orders as SHIPPED (+ send email)"

def mark_cancelled(self, request, queryset):
    n = queryset.update(status="cancelled")
    for order in queryset:
        send_cancellation_email(order)
    self.message_user(request, f"Cancelled {n} order(s) and notified customers.")
mark_cancelled.short_description = "Cancel selected orders (+ notify customer)"

# ---------- User & Profile ----------

# --- UserAdmin: add action to send birthday coupon ---
def admin_send_birthday_coupon(modeladmin, request, queryset):
    """
    Sends a coupon email to selected users (uses a fixed code).
    """
    coupon_code = getattr(settings, "BIRTHDAY_COUPON_CODE", "").strip()
    if not coupon_code:
        modeladmin.message_user(request, "No BIRTHDAY_COUPON_CODE set in settings.", level="error")
        return
    n = 0
    for user in queryset:
        send_user_birthday_coupon(user.id, coupon_code)
        n += 1
    modeladmin.message_user(request, f"Sent birthday coupon to {n} user(s).")

admin_send_birthday_coupon.short_description = "Send birthday coupon to selected users"

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
    list_display = ["user", "full_name"]

# ---------- Invoice ----------
@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("number", "order", "created_at")
    search_fields = ("number", "order__number", "order__user__email")

@admin.action(description="Mark selected orders as paid and generate invoice")
def mark_paid_and_invoice(modeladmin, request, queryset):
    for order in queryset:
        if order.status != "paid":
            order.status = "paid"
            order.save(update_fields=["status"])
            inv = generate_invoice_pdf(order)
            send_invoice_email(order)

@admin.action(description="Set DHL tracking and email invoice")
def set_tracking_and_send(modeladmin, request, queryset):
    for order in queryset:
        if not order.tracking_number:
            continue
        inv = generate_invoice_pdf(order)
        send_invoice_email(order)

@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    list_display = ("code", "kind", "value", "active", "starts_at", "ends_at", "min_subtotal")
    list_filter = ("active", "kind")
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
        return obj.product_name
    item_name.short_description = "Item"

    def unit_price_fmt(self, obj):
        return f"{obj.unit_price:.2f} €"
    unit_price_fmt.short_description = "Unit price"

    def line_total_fmt(self, obj):
        try:
            return f"{obj.line_total():.2f} €"
        except Exception:
            return "-"
    line_total_fmt.short_description = "Line total"

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    date_hierarchy = "created_at"

    # --- Use callables on the ModelAdmin to satisfy system checks ---
    list_display = ("number_col", "user", "status", "total_price_col", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("number", "user__email")
    actions = [mark_paid_and_invoice]

    readonly_fields = (
        "number_col", "subtotal_col", "shipping_fee_col", "total_price_col",
        "payment_ref_col", "payment_id_col", "created_at", "updated_at",
        "items_readonly", "delivered_at_col", "cancelled_at_col",
    )

    fields = (
        ("number_col", "user", "created_at", "delivered_at_col", "cancelled_at_col"),
        ("status", "payment_method"),
        ("tracking_number",),
        ("subtotal_col", "shipping_fee_col", "total_price_col"),
        ("shipping_address",),
        "items_readonly",
        ("payment_ref_col", "payment_id_col"),
    )

    inlines = [OrderItemInline]

    # ----- Column/callable providers -----
    def number_col(self, obj):       return obj.number
    number_col.short_description = "Number"

    def subtotal_col(self, obj):     return obj.subtotal
    subtotal_col.short_description = "Subtotal"

    def shipping_fee_col(self, obj): return obj.shipping_fee
    shipping_fee_col.short_description = "Shipping"

    def total_price_col(self, obj):  return obj.total_price
    total_price_col.short_description = "Total"

    def payment_ref_col(self, obj):  return obj.payment_ref
    payment_ref_col.short_description = "Payment ref"

    def payment_id_col(self, obj):   return obj.payment_id
    payment_id_col.short_description = "Payment id"

    def delivered_at_col(self, obj): return getattr(obj, "delivered_at", None)
    delivered_at_col.short_description = "Delivered at"

    def cancelled_at_col(self, obj): return getattr(obj, "cancelled_at", None)
    cancelled_at_col.short_description = "Cancelled at"

    def items_readonly(self, obj):
        rows = [
            (f"{oi.product_name} × {oi.quantity}", f"{oi.unit_price:.2f} €", f"{oi.line_total():.2f} €")
            for oi in obj.items.all()
        ]
        return format_html(
            "<table style='width:100%'><tr><th align='left'>Item</th>"
            "<th align='right'>Unit</th><th align='right'>Total</th></tr>{}</table>",
            format_html_join("", "<tr><td>{}</td><td align='right'>{}</td><td align='right'>{}</td></tr>", rows)
        )
    items_readonly.short_description = "Items"

# ---------- OrderItem ----------
@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    # Provide callables to satisfy checks
    list_display = ("product", "quantity", "unit_price_col", "line_total_col")
    search_fields = ("product__title_en", "product__title")

    def unit_price_col(self, obj): return obj.unit_price
    unit_price_col.short_description = "Unit price"

    def line_total_col(self, obj):
        try:
            return obj.line_total()
        except Exception:
            return "-"
    line_total_col.short_description = "Line total"

# ---------- ReturnRequest ----------
class ReturnAttachmentInline(admin.TabularInline):
    model = ReturnAttachment
    extra = 0

@admin.register(ReturnRequest)
class ReturnRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "user", "request_type", "status", "created_at")
    list_filter = ("request_type", "status", "created_at")
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
    # Provide callables for price/quantity to satisfy checks
    list_display = ["title_en", "category", "price_col", "quantity_col"]
    search_fields = ("title_en", "title_de")

    def price_col(self, obj): return obj.price
    price_col.short_description = "Price"

    def quantity_col(self, obj): return obj.quantity
    quantity_col.short_description = "Qty"

@admin.register(NaturalStone)
class NaturalStoneAdmin(admin.ModelAdmin):
    list_display = ["type"]
    inlines = [NaturalStoneImageInline]

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


admin.site.register(NaturalStoneImage)
admin.site.register(ContactMessage)
admin.site.register(NewsletterSubscriber)
admin.site.register(Review)
admin.site.register(StoneEnquiry)
admin.site.register(Wishlist)

