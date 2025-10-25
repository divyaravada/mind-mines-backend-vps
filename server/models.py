# server/models.py
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.contrib.postgres.fields import ArrayField
from django.db import models, transaction
from django.db.models import Q, UniqueConstraint, F
from django.db.models.functions import Lower
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.utils import timezone
from django.conf import settings

from decimal import Decimal, ROUND_HALF_UP
from datetime import timedelta, date
import secrets
import uuid


STONE_TYPES = [
    ("marble", "Marble"),
    ("granite", "Granite"),
    ("limestone", "Limestone"),
    ("sandstone", "Sandstone"),
    ("slate", "Slate"),
    ("quartzite", "Quartzite"),
    ("travertine", "Travertine"),
    ("soapstone", "Soapstone"),
]
CATEGORY_CHOICES = [
    ("home_decor", "Home Decor & Accessories"),
    ("storage", "Storage & Organization"),
    ("kitchen", "Kitchen & Appliances"),
    ("office", "Office Supplies"),
    ("bathroom", "Bathroom Accessories"),
    ("sculpture", "Sculptures & Customization"),
]

CATEGORY_CODES = {
    "home_decor": "HOME",
    "storage": "STOR",
    "kitchen": "KITCH",
    "office": "OFF",
    "bathroom": "BATH",
    "sculpture": "SCULPT",
}

MATERIAL_CHOICES = [
        ("marble", "Marble"),
        ("granite", "Granite"),
        ("stone", "Stone"),
        # Add more as needed
    ]

COLOR_CHOICES = [
        ("white", "White"),
        ("black", "Black"),
        ("green", "Green"),
        ("grey", "Grey"),
        # Add more as needed
    ]

VAT_RATE = Decimal("0.19")

def _q2(d: Decimal) -> Decimal :
    return Decimal(d or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ----------------------------
# Users
# ----------------------------
    
class CustomUserManager(BaseUserManager):
    use_in_migrations = True
    def _create_user(self, email, password, **extra):
        if not email: raise ValueError("Email must be set")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra)
        user.set_password(password); user.save(using=self._db)
        return user
    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False); extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)
    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True); extra.setdefault("is_superuser", True)
        if extra.get("is_staff") is not True: raise ValueError("Superuser must have is_staff=True.")
        if extra.get("is_superuser") is not True: raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra)


class User(AbstractUser):
    # keep an optional username field so admin can fill it if desired
    username = None  # <-- IMPORTANT
    email = models.EmailField(unique=True)
    # optional display handle if you want:
    display_name = models.CharField(max_length=100, blank=True, null=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []
    objects = CustomUserManager()
    def __str__(self): return self.email

class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    full_name = models.CharField(max_length=255, blank=True)
    mobile = models.CharField(max_length=20, blank=True)
    image = models.ImageField(upload_to="profile_pics/", blank=True, null=True)
    verified = models.BooleanField(default=False)
    stripe_customer_id = models.CharField(max_length=64, blank=True, null=True)
    date_of_birth = models.DateField(blank=True, null=True) 

    def __str__(self):
        return f"{self.user.email} profile"


class Address(models.Model):
    user = models.ForeignKey("server.User", on_delete=models.CASCADE, related_name="addresses", null=True, blank=True)
    # UI-first fields
    first_name = models.CharField(max_length=100, blank=True)
    last_name  = models.CharField(max_length=100, blank=True)
    address_line1 = models.CharField(max_length=255, blank=True)
    apartment = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    city = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    # Legacy (kept)
    full_name = models.CharField(max_length=255, blank=True)
    house_number = models.CharField(max_length=50, blank=True)
    street_name = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=100, blank=True)
    pincode = models.CharField(max_length=20, blank=True)
    # UX
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        constraints = [
            UniqueConstraint(fields=["user"], condition=Q(is_default=True), name="one_default_address_per_user"),
        ]
    def set_default_for_user(self):
        Address.objects.filter(user=self.user, is_default=True).exclude(pk=self.pk).update(is_default=False)
        if not self.is_default:
            self.is_default = True; self.save(update_fields=["is_default"])
    def __str__(self):
        user_email = getattr(self.user, "email", "Guest")
        name = " ".join(filter(None, [self.first_name, self.last_name])) or self.full_name or user_email
        line1 = self.address_line1 or " ".join([self.street_name, self.house_number]).strip() or "Address"
        return f"{name} — {line1}"
    
# ----------------------------
# Stones / Content
# ----------------------------    
class NaturalStone(models.Model):
    type = models.CharField(max_length=30, choices=STONE_TYPES, unique=True)
    description_en = models.TextField(); description_de = models.TextField()
    interest_en = models.TextField(); interest_de = models.TextField()
    key_features_en = models.TextField(); key_features_de = models.TextField()
    usage_en = models.TextField(); usage_de = models.TextField()
    image = models.ImageField(upload_to="natural-stone/images/", blank=True, null=True)
    def __str__(self): return self.type

class NaturalStoneImage(models.Model):
    stone = models.ForeignKey(NaturalStone, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to="natural-stone/images/")
    def save(self, *a, **kw):
        if self.pk is None and NaturalStoneImage.objects.filter(stone=self.stone).count() >= 10:
            raise ValidationError("A stone can have at most 10 gallery images.")
        return super().save(*a, **kw)
    def __str__(self): return f"Image for {self.stone.type}"

class StoneEnquiry(models.Model):
    stone_type = models.CharField(max_length=30)
    name = models.CharField(max_length=100)
    email = models.EmailField()
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    def __str__(self): return f"{self.name} - {self.stone_type}"

# ----------------------------
# Catalog
# ----------------------------
     
class Product(models.Model):
    # EN/DE content
    title_en = models.CharField(max_length=255)
    title_de = models.CharField(max_length=255)
    short_description1_en = models.CharField(max_length=255, blank=True)
    short_description1_de = models.CharField(max_length=255, blank=True)
    short_description2_en = models.CharField(max_length=255, blank=True)
    short_description2_de = models.CharField(max_length=255, blank=True)
    short_description3_en = models.CharField(max_length=255, blank=True)
    short_description3_de = models.CharField(max_length=255, blank=True)
    descriptionpara1_en = models.TextField(blank=True); descriptionpara1_de = models.TextField(blank=True)
    descriptionpara2_en = models.TextField(blank=True); descriptionpara2_de = models.TextField(blank=True)
    descriptionpara3_en = models.TextField(blank=True); descriptionpara3_de = models.TextField(blank=True)
    carepara_en = models.TextField(blank=True); carepara_de = models.TextField(blank=True)
    delivery_en = models.TextField(blank=True); delivery_de = models.TextField(blank=True)
    returnspara1_en = models.TextField(blank=True); returnspara1_de = models.TextField(blank=True)
    returnspara2_en = models.TextField(blank=True); returnspara2_de = models.TextField(blank=True)
    returnspara3_en = models.TextField(blank=True); returnspara3_de = models.TextField(blank=True)

    type_of_stone = models.CharField(max_length=50, choices=STONE_TYPES)
    material = models.CharField(max_length=20, choices=MATERIAL_CHOICES, default="marble")
    color = models.CharField(max_length=20, choices=COLOR_CHOICES, default="white")
    stone_color_image = models.ImageField(upload_to="decor-products/colors/", null=True, blank=True)

    # Physicals
    dimensions = models.CharField(max_length=255, blank=True)
    weight = models.CharField(max_length=100, blank=True)

    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES, default="home_decor")

    # ✅ store GROSS prices (incl. VAT 19%) to match storefront labels
    price_gross = models.DecimalField(max_digits=10, decimal_places=2)              # was price
    discount_gross = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))  # optional absolute discount
    sale_price_gross = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))  # computed on save
    vat_rate = models.DecimalField(max_digits=4, decimal_places=2, default=VAT_RATE)

    # Stock
    stock = models.PositiveIntegerField(default=0)   # unified; removed quantity
    key = models.CharField(max_length=20, unique=True, null=True, blank=True)

    image = models.ImageField(upload_to="decor-products/", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta: ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # product key like HOME01 etc.
        if not self.key:
            cat_prefix = CATEGORY_CODES.get(self.category, "MIND")
            self.key = f"{cat_prefix}{str(self.id).zfill(2)}"
            super().save(update_fields=["key"])
        # compute sale price
        new_sale = max(self.price_gross - self.discount_gross, Decimal("0.00"))
        if self.sale_price_gross != new_sale:
            self.sale_price_gross = _q2(new_sale)
            super().save(update_fields=["sale_price_gross"])

    @property
    def is_discounted(self):
        return self.discount_gross > 0 and self.sale_price_gross < self.price_gross

    def __str__(self): return self.title_en

class ProductMedia(models.Model):
    MEDIA_TYPE_CHOICES = [("image", "Image"), ("video", "Video")]
    product = models.ForeignKey(Product, related_name="media", on_delete=models.CASCADE)
    image = models.ImageField(upload_to="product-gallery/", null=True, blank=True)
    video = models.FileField(upload_to="product-videos/", null=True, blank=True)
    caption = models.CharField(max_length=255, blank=True)
    order = models.PositiveIntegerField(default=0)
    media_type = models.CharField(max_length=10, choices=MEDIA_TYPE_CHOICES, default="image")

    def clean(self):
        # exactly one of (image, video)
        if bool(self.image) == bool(self.video):
            raise ValidationError("Provide exactly one of image or video.")
        # enforce per-product caps
        if self.pk is None:
            if self.media_type == "image":
                n = ProductMedia.objects.filter(product=self.product, media_type="image").count()
                if n >= 10: raise ValidationError("A product can have at most 10 images.")
            else:
                n = ProductMedia.objects.filter(product=self.product, media_type="video").count()
                if n >= 10: raise ValidationError("A product can have at most 10 videos.")

    def __str__(self):
        return f"{self.media_type.capitalize()} for {self.product}"

   
# ---------- extras ----------
class StockAlert(models.Model):
    product = models.ForeignKey("Product", related_name="stock_alerts", on_delete=models.CASCADE)
    email = models.EmailField(validators=[EmailValidator()], db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    unsubscribe_token = models.CharField(max_length=64, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    notified_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        unique_together = ("product", "email")
        indexes = [models.Index(fields=["product","is_active"]), models.Index(fields=["email"])]
    def save(self, *a, **k):
        if not self.unsubscribe_token: self.unsubscribe_token = secrets.token_urlsafe(32)
        if self.email: self.email = self.email.strip().lower()
        super().save(*a, **k)
    def mark_notified(self):
        self.is_active = False; self.notified_at = timezone.now()
        self.save(update_fields=["is_active","notified_at"])

class Wishlist(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["-id"]
        unique_together = ("user","product")

class ContactMessage(models.Model):
    name = models.CharField(max_length=200)
    email = models.EmailField()
    message = models.TextField()
    sent_at = models.DateTimeField(auto_now_add=True)
    def __str__(self): return f"{self.name} - {self.email}"

class NewsletterSubscriber(models.Model):
    email = models.EmailField(unique=True)
    subscribed_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [UniqueConstraint(Lower("email"), name="uniq_newsletter_email_ci")]
    def __str__(self): return self.email



ORDER_STATUS = [
    ("pending_payment", "pending_payment"),
    ("paid", "paid"),
    ("processing", "processing"),
    ("shipped", "shipped"),
    ("delivered", "delivered"),
    ("completed", "completed"),
    ("payment_failed", "payment_failed"),
    ("awaiting_bank_transfer", "awaiting_bank_transfer"),
    ("canceled", "canceled"),
    ("refunded", "refunded"),
]

class Order(models.Model):
    PAYMENT_METHODS = [
        ("stripe_card", "Stripe Card"),
        ("stripe_sepa", "Stripe SEPA Direct Debit"),
        ("paypal", "PayPal"),
        ("manual_bank", "Manual Bank Transfer"),
    ]
    user = models.ForeignKey("server.User", on_delete=models.SET_NULL, related_name="orders", null=True, blank=True)
    email = models.EmailField()  # always set (for users, copy from account; for guests, their email)
    guest_name  = models.CharField(max_length=200, blank=True, null=True)

    # 🔒 snapshot addresses (keep FKs if you want, but snapshots are source of truth)
    shipping_address = models.ForeignKey('server.Address', on_delete=models.SET_NULL, null=True, blank=True)
    shipping_address_json = models.JSONField(default=dict)   # <- snapshot
    billing_address_json = models.JSONField(default=dict)    # <- snapshot

    # numbers & currency
    order_number = models.CharField(max_length=20, unique=True)  # e.g., MM00000001
    currency = models.CharField(max_length=3, default="eur")

    # totals (gross-based)
    subtotal_gross = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    vat_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    shipping_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    total_gross = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    # status & payments
    payment_method = models.CharField(max_length=32, choices=PAYMENT_METHODS, default="stripe_card")
    status = models.CharField(max_length=32, choices=ORDER_STATUS, default="pending_payment")
    remarks = models.TextField(blank=True, default="")
    installments_months = models.PositiveIntegerField(null=True, blank=True)

    stripe_payment_intent = models.CharField(max_length=128, blank=True, default="")
    stripe_customer = models.CharField(max_length=128, blank=True, default="")
    sepa_mandate_id = models.CharField(max_length=128, blank=True, default="")

    tracking_number = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)

    # stock management flags
    stock_locked = models.BooleanField(default=False)
    stock_released = models.BooleanField(default=False)

    # guest access
    lookup_token = models.CharField(max_length=36, blank=True, default="")

    class Meta:
        indexes = [models.Index(fields=["order_number"]), models.Index(fields=["status","created_at"])]

    def __str__(self): return f"Order {self.order_number}"

    def save(self, *args, **kwargs):
        creating = self.pk is None
        super().save(*args, **kwargs)
        if creating:
            # ensure a unique, human order number after PK exists
            if not self.order_number:
                self.order_number = f"MM{self.pk:08d}"
                super().save(update_fields=["order_number"])
            if not self.lookup_token:
                self.lookup_token = str(uuid.uuid4())
                super().save(update_fields=["lookup_token"])

    @property
    def return_deadline(self):
        if not self.delivered_at: return None
        return self.delivered_at + timedelta(days=15)  # ← 15 days window

    @transaction.atomic
    def reserve_stock(self):
        """Reserve (decrease) stock at order creation or payment success."""
        if self.stock_locked: return
        for it in self.items.select_related("product").select_for_update():
            p = it.product
            if p.stock < it.quantity:
                raise ValueError(f"Insufficient stock for {p} (have {p.stock}, need {it.quantity})")
            p.stock = F("stock") - it.quantity
            p.save(update_fields=["stock"])
        self.stock_locked = True
        self.save(update_fields=["stock_locked"])

    @transaction.atomic
    def release_stock(self):
        """Release (increase) stock if order canceled/failed and not already released."""
        if not self.stock_locked or self.stock_released: return
        for it in self.items.select_related("product").select_for_update():
            p = it.product
            p.stock = F("stock") + it.quantity
            p.save(update_fields=["stock"])
        self.stock_released = True
        self.save(update_fields=["stock_released"])

class OrderItem(models.Model):
    order = models.ForeignKey(Order, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="order_items")
    quantity = models.PositiveIntegerField()
    unit_price_gross = models.DecimalField(max_digits=10, decimal_places=2)  # snapshot gross
    vat_rate = models.DecimalField(max_digits=4, decimal_places=2, default=VAT_RATE)
    def line_total_gross(self): return _q2(self.unit_price_gross * self.quantity)
    @property
    def product_name(self):
        try: return getattr(self.product, "title_en", None) or str(self.product)
        except Exception: return "Product"
    def __str__(self): return f"{self.product_name} x {self.quantity}"
    
#--------------Coupon code------------

DISCOUNT_TYPES = [("percent", "Percent"), ("fixed", "Fixed amount")]

class Coupon(models.Model):
    code = models.CharField(max_length=32, unique=True, db_index=True)
    kind = models.CharField(max_length=10, choices=DISCOUNT_TYPES, default="percent")
    value = models.DecimalField(max_digits=10, decimal_places=2)
    active = models.BooleanField(default=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at   = models.DateTimeField(null=True, blank=True)
    min_subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    max_uses = models.PositiveIntegerField(null=True, blank=True)
    max_uses_per_user = models.PositiveIntegerField(null=True, blank=True)
    products = models.ManyToManyField("Product", blank=True, related_name="coupons")
    categories = ArrayField(models.CharField(max_length=30), default=list, blank=True)
    max_discount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True); updated_at = models.DateTimeField(auto_now=True)
    class Meta: ordering = ["-created_at","code"]
    def __str__(self): return self.code.upper()

    # ---- helpers
    def is_time_ok(self, now=None):
        now = now or timezone.now()
        if self.starts_at and now < self.starts_at: return False
        if self.ends_at and now > self.ends_at:     return False
        return True

    def eligible_items_subtotal(self, items):
        """
        items: list of dicts with product_id & qty OR OrderItem qs.
        Returns gross (VAT-included) subtotal of eligible items.
        """
        subtotal = Decimal("0.00")
        # prefetch target sets
        allowed_products = set(self.products.values_list("id", flat=True)) if self.pk else set()
        allowed_categories = set((self.categories or []))

        for it in items:
            # item can be {'product_id': x, 'qty': y} or OrderItem instance
            if hasattr(it, "product_id"):
                pid = it.product_id
                qty = it.quantity
                unit = it.unit_price  # already gross in your app
                cat = it.product.category
            else:
                pid = int(it.get("product_id"))
                qty = int(it.get("qty"))
                p = Product.objects.get(pk=pid)
                unit = Product.vat_included_price(p)  # gross
                cat = p.category

            ok = True
            if allowed_products:
                ok = pid in allowed_products
            if ok and allowed_categories:
                ok = cat in allowed_categories

            if ok:
                subtotal += (unit * qty)

        return subtotal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def can_apply(self, user, items, cart_subtotal):
        """
        Return (ok, reason). Usage limits are checked against PAID redemptions.
        """
        if not self.active:
            return False, "Coupon is not active."
        if not self.is_time_ok():
            return False, "Coupon is not currently valid."
        if cart_subtotal < self.min_subtotal:
            return False, f"Minimum order amount is {self.min_subtotal:.2f} €."

        # usage limits
        paid_q = CouponRedemption.objects.filter(coupon=self, order__status="paid")
        if self.max_uses is not None and paid_q.count() >= self.max_uses:
            return False, "Coupon usage limit reached."
        if user and self.max_uses_per_user is not None:
            from server.models import Order as O  # local import to avoid cycles
            user_paid = paid_q.filter(user=user).count()
            # Also block if the user already has an open order using this coupon
            has_open = O.objects.filter(
                user=user, coupon=self
            ).exclude(status__in=["cancelled", "failed"]).exists()
            if user_paid >= self.max_uses_per_user or has_open:
                return False, "You have already used this coupon."

        # if targeted and nothing matches, block
        elig = self.eligible_items_subtotal(items)
        if (self.products.exists() or (self.categories or [])) and elig <= 0:
            return False, "Coupon does not apply to selected products."

        return True, ""

    def discount_amount(self, user, items, cart_subtotal):
        """
        Compute discount in EUR (gross), capped and only on eligible subtotal if targeted.
        """
        elig_subtotal = self.eligible_items_subtotal(items)
        base = elig_subtotal if (self.products.exists() or (self.categories or [])) else cart_subtotal
        if base <= 0:
            return Decimal("0.00")
        if self.kind == "percent":
            disc = (base * (self.value / Decimal("100")))
        else:
            disc = Decimal(self.value)

        if self.max_discount:
            disc = min(disc, self.max_discount)

        disc = min(disc, base)  # never exceed base
        return disc.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class CouponRedemption(models.Model):
    coupon = models.ForeignKey(Coupon, on_delete=models.CASCADE, related_name="redemptions")
    user   = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="coupon_redemptions")  # ← allow guest
    order  = models.OneToOneField("Order", on_delete=models.CASCADE, related_name="coupon_redemption")
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        unique_together = (("coupon","order"),)
        indexes = [models.Index(fields=["coupon","user"])]

#------------------------------Promotion----------------

class Promotion(models.Model):
    name = models.CharField(max_length=64)
    coupon = models.ForeignKey("Coupon", null=True, blank=True, on_delete=models.SET_NULL, related_name="promotions")
    code = models.CharField(max_length=32, blank=True)
    headline = models.CharField(max_length=120)
    subheadline = models.CharField(max_length=180, blank=True)
    start_at = models.DateTimeField()
    end_at   = models.DateTimeField()
    is_enabled = models.BooleanField(default=True)
    bg_color = models.CharField(max_length=16, default="#111827")
    text_color = models.CharField(max_length=16, default="#ffffff")
    link_url = models.URLField(blank=True)
    image = models.ImageField(upload_to="promos/", null=True, blank=True)
    country_whitelist = ArrayField(models.CharField(max_length=2), default=list, blank=True)
    priority = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True); updated_at = models.DateTimeField(auto_now=True)
    class Meta: ordering = ["-priority","-updated_at"]
    def __str__(self): return self.name
    
# ----------Order Returns ----------
class ReturnRequest(models.Model):
    REQUEST_TYPES = (("cancel","Cancel"), ("return","Return"))
    STATUS = [
        ("requested", "Requested"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("received", "Received"),
        ("refunded", "Refunded"),
    ]
    order = models.ForeignKey("Order", on_delete=models.CASCADE, related_name="return_requests")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="return_requests")
    message = models.TextField(blank=True, default="")
    status = models.CharField(max_length=16, choices=STATUS, default="requested")
    request_type = models.CharField(max_length=16, choices=REQUEST_TYPES)
    created_at = models.DateTimeField(auto_now_add=True)

class ReturnAttachment(models.Model):
    request = models.ForeignKey(ReturnRequest, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to="returns/%Y/%m/")
    uploaded_at = models.DateTimeField(auto_now_add=True)



# ---------- invoices ----------
class Invoice(models.Model):
    order = models.OneToOneField("Order", on_delete=models.CASCADE, related_name="invoice")
    number = models.CharField(max_length=32, unique=True)
    pdf = models.FileField(upload_to="invoices/")
    created_at = models.DateTimeField(auto_now_add=True)
    def __str__(self): return self.number

# ---------- reviews ----------
class Review(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reviews")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="reviews")
    rating = models.PositiveIntegerField()
    message = models.TextField()
    image = models.ImageField(upload_to="review_images/", blank=True, null=True)
    author_name = models.CharField(max_length=100, blank=True, default="")
    author_avatar = models.ImageField(upload_to="review_avatars/", blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    def __str__(self): return f"{self.user} on {self.product} ({self.rating})"
