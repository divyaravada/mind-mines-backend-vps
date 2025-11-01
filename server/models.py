# server/models.py
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.contrib.postgres.fields import ArrayField
from django.db import models, transaction
from django.db.models import Q, UniqueConstraint, F
from django.db.models.functions import Lower
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
from django.conf import settings

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import timedelta
import secrets
import uuid

# ----------------- Constants -----------------

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

def _q2(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

# ----------------- Users ----------------
#  
class CustomUserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("The Email must be set")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra_fields)
    
class User(AbstractUser):
    # Optional username (email is the login identifier)
    email = models.EmailField(unique=True)
    username = models.CharField(max_length=100, blank=True, null=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = CustomUserManager()

    def __str__(self):
        return self.email

class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    full_name = models.CharField(max_length=255, blank=True)
    mobile = models.CharField(max_length=20, blank=True)
    image = models.ImageField(upload_to="profile_pics/", blank=True, null=True)
    verified = models.BooleanField(default=False)
    stripe_customer_id = models.CharField(max_length=64, blank=True, null=True)

    def __str__(self):
        return f"{self.user.email} profile"

# ----------------- Addresses -----------------

class Address(models.Model):
    user = models.ForeignKey("server.User", on_delete=models.CASCADE, related_name='addresses', null=True, blank=True)

    # UI-first fields
    first_name = models.CharField(max_length=100, blank=True)
    last_name  = models.CharField(max_length=100, blank=True)
    address_line1 = models.CharField(max_length=255, blank=True)  # “Address”
    apartment = models.CharField(max_length=100, blank=True)      # “Address supplement”
    postal_code = models.CharField(max_length=20, blank=True)
    city = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=20, blank=True)

    # Legacy (kept for compatibility)
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
            UniqueConstraint(
                fields=["user"],
                condition=Q(is_default=True),
                name="one_default_address_per_user",
            ),
        ]

    def save(self, *args, **kwargs):
        # If adding a new default address, create it as non-default, then flip.
        if self._state.adding and self.is_default:
            make_default = True
            self.is_default = False
            super().save(*args, **kwargs)
            # now safely flip with DB lock and clearing others
            self.set_default_for_user()
            return

        # Updates: if turning an existing address into default, clear others first
        if self.user_id and self.is_default and not self._state.adding:
            with transaction.atomic():
                (Address.objects
                 .select_for_update()
                 .filter(user_id=self.user_id, is_default=True)
                 .exclude(pk=self.pk)
                 .update(is_default=False))
        super().save(*args, **kwargs)

    def set_default_for_user(self):
        with transaction.atomic():
            (Address.objects
             .select_for_update()
             .filter(user=self.user, is_default=True)
             .exclude(pk=self.pk)
             .update(is_default=False))
            if not self.is_default:
                self.is_default = True
                super().save(update_fields=["is_default"])
                
        Address.objects.filter(user=self.user, is_default=True).exclude(pk=self.pk).update(is_default=False)
        if not self.is_default:
            self.is_default = True
            self.save(update_fields=["is_default"])

    def __str__(self):
        user_email = getattr(self.user, "email")
        name = " ".join(filter(None, [self.first_name, self.last_name])) or self.full_name or user_email
        line1 = self.address_line1 or " ".join([self.street_name, self.house_number]).strip() or "Address"
        return f"{name} — {line1}"
    
# ----------------- Natural Stones -----------------

class NaturalStone(models.Model):
    type = models.CharField(max_length=30, choices=STONE_TYPES, unique=True)
    description_en = models.TextField()
    description_de = models.TextField()
    interest_en = models.TextField()
    interest_de = models.TextField()
    key_features_en = models.TextField()
    key_features_de = models.TextField()
    usage_en = models.TextField()
    usage_de = models.TextField()
    image = models.ImageField(upload_to="natural-stone/images/", blank=True, null=True, help_text="Primary display image")

    def __str__(self):
        return self.type

class NaturalStoneImage(models.Model):
    stone = models.ForeignKey(NaturalStone, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to="natural-stone/images/")

    def save(self, *args, **kwargs):
        if self.pk is None:
            current = NaturalStoneImage.objects.filter(stone=self.stone).count()
            if current >= 10:
                raise ValidationError("A stone can have at most 10 gallery images.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"Image for {self.stone.type}"

class StoneEnquiry(models.Model):
    stone_type = models.CharField(max_length=30)
    name = models.CharField(max_length=100)
    email = models.EmailField()
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} - {self.stone_type}"

 # ----------------- Products & Media -----------------

def _q2(x: Decimal) -> Decimal:
    return Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

class Product(models.Model):
    # Titles
    title_en = models.CharField(max_length=255, default="Default Title")
    title_de = models.CharField(max_length=255, default="Default Title")

    # 3 short lines
    short_description1_en = models.CharField(max_length=255, blank=True)
    short_description1_de = models.CharField(max_length=255, blank=True)
    short_description2_en = models.CharField(max_length=255, blank=True)
    short_description2_de = models.CharField(max_length=255, blank=True)
    short_description3_en = models.CharField(max_length=255, blank=True)
    short_description3_de = models.CharField(max_length=255, blank=True)

    # 3 paragraphs
    descriptionpara1_en = models.TextField(blank=True)
    descriptionpara1_de = models.TextField(blank=True)
    descriptionpara2_en = models.TextField(blank=True)
    descriptionpara2_de = models.TextField(blank=True)
    descriptionpara3_en = models.TextField(blank=True)
    descriptionpara3_de = models.TextField(blank=True)

    # care / delivery / returns
    carepara_en = models.TextField(blank=True)
    carepara_de = models.TextField(blank=True)
    delivery_en = models.TextField(blank=True)
    delivery_de = models.TextField(blank=True)
    returnspara1_en = models.TextField(blank=True)
    returnspara1_de = models.TextField(blank=True)
    returnspara2_en = models.TextField(blank=True)
    returnspara2_de = models.TextField(blank=True)
    returnspara3_en = models.TextField(blank=True)
    returnspara3_de = models.TextField(blank=True)

    # attributes
    type_of_stone = models.CharField(max_length=50, choices=STONE_TYPES, blank=True, null=True)
    material = models.CharField(max_length=20, choices=MATERIAL_CHOICES, default="marble", blank=True, null=True)
    color = models.CharField(max_length=20, choices=COLOR_CHOICES, default="white", blank=True, null=True)
    stone_color_image = models.ImageField(upload_to="decor-products/colors/", null=True, blank=True)

    # inventory
    quantity = models.PositiveIntegerField(default=0)  # authoritative stock count

    # misc
    dimensions = models.CharField(max_length=255, blank=True)
    weight = models.CharField(max_length=100, blank=True)
    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES, default="home_decor")

    # pricing (GROSS / incl. 19% VAT)
    price_net = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    vat_rate = models.DecimalField(
        max_digits=4, decimal_places=4, default=Decimal("0.19"),
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("1.00"))],
        help_text="VAT as a fraction (0.19 = 19%)."
    )
    price_gross = models.DecimalField(max_digits=10, decimal_places=2)  # <-- always incl. VAT
    sale_price_gross = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    discount_percent = models.PositiveIntegerField(blank=True, null=True)  # e.g. 10 = 10%

    key = models.CharField(max_length=20, unique=True, null=True, blank=True)
    image = models.ImageField(upload_to="decor-products/", null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def clean(self):
        if self.sale_price_gross is not None:
            try:
                sale = Decimal(self.sale_price_gross)
                if sale <= 0:
                    self.sale_price_gross = None
            except (InvalidOperation, TypeError):
                self.sale_price_gross = None

        # Sale must be strictly lower than base gross (if provided)
        if self.sale_price_gross is not None and self.price_gross is not None:
            if Decimal(self.sale_price_gross) >= Decimal(self.price_gross):
                raise ValidationError({"sale_price_gross": "Sale price must be lower than base gross price."})

        # Discount 0..99 (avoid 100%)
        if self.discount_percent is not None:
            if not (0 <= int(self.discount_percent) <= 99):
                raise ValidationError({"discount_percent": "Discount percent must be between 0 and 99."})

        # Guard VAT range
        if self.vat_rate is None:
            self.vat_rate = Decimal("0.19")
        else:
            vr = Decimal(self.vat_rate)
            if vr < 0 or vr > 1:
                raise ValidationError({"vat_rate": "VAT must be between 0 and 1 (e.g., 0.19 for 19%)."})

    def save(self, *args, **kwargs):
        self.clean()
        if self.price_net is not None:
            net = Decimal(self.price_net)
            self.price_gross = _q2(net * (Decimal(1) + Decimal(self.vat_rate)))

        # If only gross present, back-calc net (nice UX)
        elif self.price_gross is not None and self.price_net is None:
            gross = Decimal(self.price_gross)
            denom = (Decimal(1) + Decimal(self.vat_rate))
            if denom > 0:
                self.price_net = _q2(gross / denom)

        creating = self.pk is None
        super().save(*args, **kwargs)

        # your existing key assignment logic unchanged...
        if (creating or not self.key) and self.pk:
            cat_prefix = CATEGORY_CODES.get(self.category, "MIND")
            new_key = f"{cat_prefix}{str(self.id).zfill(2)}"
            if self.key != new_key:
                self.key = new_key
                super().save(update_fields=["key"])

    @property
    def low_stock(self) -> bool:
        return 0 < int(self.quantity or 0) < 5

    @property
    def out_of_stock(self) -> bool:
        return int(self.quantity or 0) <= 0

    def stock_message(self) -> str:
        q = int(self.quantity or 0)
        if q <= 0:
            return "Out of stock — new batch arriving soon"
        if q < 5:
            return "Hurry — almost gone!"
        return "In stock"

    @property
    def is_discounted(self) -> bool:
        if self.sale_price_gross is not None and self.price_gross is not None:
            return Decimal(self.sale_price_gross) < Decimal(self.price_gross)
        return bool(self.discount_percent and self.discount_percent > 0)


    @property
    def final_price_gross(self) -> Decimal:
        base = Decimal(self.price_gross or 0)
        sale = Decimal(self.sale_price_gross or 0) if self.sale_price_gross is not None else None
        if sale is not None and sale > 0 and sale < base:
            return _q2(sale)
        if self.discount_percent:
            dp = Decimal(self.discount_percent) / Decimal(100)
            return _q2(base * (Decimal(1) - dp))
        return _q2(base)

    def __str__(self):
        return self.title_en

class ProductMedia(models.Model):
    MEDIA_TYPE_CHOICES = [("image", "Image"), ("video", "Video")]
    product = models.ForeignKey(Product, related_name="media", on_delete=models.CASCADE)
    image = models.ImageField(upload_to="product-gallery/", null=True, blank=True)
    video = models.FileField(upload_to="product-videos/", null=True, blank=True)
    caption = models.CharField(max_length=255, blank=True)
    order = models.PositiveIntegerField(default=0)
    media_type = models.CharField(max_length=10, choices=MEDIA_TYPE_CHOICES, default="image")

    def save(self, *args, **kwargs):
        if self.pk is None:
            # enforce 10 images and 10 videos per product
            if self.media_type == "image":
                cnt = ProductMedia.objects.filter(product=self.product, media_type="image").count()
                if cnt >= 10:
                    raise ValidationError("Each product can have at most 10 images.")
            if self.media_type == "video":
                cnt = ProductMedia.objects.filter(product=self.product, media_type="video").count()
                if cnt >= 10:
                    raise ValidationError("Each product can have at most 10 videos.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.media_type.capitalize()} for {self.product}"


# ----------------- Wishlist & notifications -----------------
class OnDemandOrderRequest(models.Model):
    product = models.ForeignKey("Product", related_name="on_demand_requests", on_delete=models.CASCADE)
    name = models.CharField(max_length=120, blank=True)
    email = models.EmailField()
    quantity = models.PositiveIntegerField(default=1)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        indexes = [
            models.Index(fields=["product", "created_at"]),
            models.Index(fields=["email"]),
        ]

    def __str__(self):
        return f"On-demand request #{self.id} for {self.product_id} x{self.quantity}"
    
class StockAlert(models.Model):
    """Notify me when back in stock."""
    product = models.ForeignKey("Product", related_name="stock_alerts", on_delete=models.CASCADE)
    email = models.EmailField(validators=[EmailValidator()], db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    unsubscribe_token = models.CharField(max_length=64, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("product", "email")
        indexes = [
            models.Index(fields=["product", "is_active"]),
            models.Index(fields=["email"]),
        ]

    def save(self, *args, **kwargs):
        if not self.unsubscribe_token:
            self.unsubscribe_token = secrets.token_urlsafe(32)
        if self.email:
            self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    def mark_notified(self):
        self.is_active = False
        self.notified_at = timezone.now()
        self.save(update_fields=["is_active", "notified_at"])

class Wishlist(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        unique_together = ("user", "product")


# ----------------- Contact & Newsletter -----------------

class ContactMessage(models.Model):
    name = models.CharField(max_length=200)
    email = models.EmailField()
    message = models.TextField()
    sent_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} - {self.email}"

class NewsletterSubscriber(models.Model):
    email = models.EmailField(unique=True)
    subscribed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            UniqueConstraint(Lower("email"), name="uniq_newsletter_email_ci")
        ]

    def __str__(self):
        return self.email

# ----------------- Orders, Items, Returns -----------------

ORDER_STATUS = [
    ("pending", "Pending"),
    ("processing", "Processing"),
    ("paid", "Paid"),
    ("shipped", "Shipped"),
    ("delivered", "Delivered"),
    ("cancelled", "Cancelled"),
    ("failed", "Failed"),
    ("refunded", "Refunded"),
]

class Order(models.Model):
    PAYMENT_METHODS = [
        ("card", "Card"),
        ("card_installments", "Card Installments"),
        ("sepa_debit", "SEPA Direct Debit"),
        ("sepa_debit_installments", "SEPA Direct Debit (Installments)"),
        ("paypal", "PayPal"),
        ("bank_transfer", "Bank Transfer (Manual Direct Debit)"),
        ("giropay", "Giropay"),
        ("klarna", "Klarna"),
    ]
    user = models.ForeignKey(
        "server.User",
        on_delete=models.CASCADE,
        related_name='orders',
        null=True,
        blank=True
    )

    # the email we will use for confirmations + invoice
    email = models.EmailField()

    # legacy (optional single-line)
    product = models.ForeignKey("server.Product", on_delete=models.SET_NULL, null=True, blank=True)
    quantity = models.PositiveIntegerField(null=True, blank=True)

    payment_method = models.CharField(max_length=32, choices=PAYMENT_METHODS, default="card")
    status = models.CharField(max_length=32, choices=ORDER_STATUS, default="pending")

    payment_ref = models.CharField(max_length=128, blank=True, default="")  # Stripe PI / Schedule ID / PayPal order ID
    payment_id = models.CharField(max_length=128, blank=True, default="")   # Stripe charge ID etc.
    tracking_number = models.CharField(max_length=64, blank=True, default="")

    invoice_number = models.CharField(max_length=32, blank=True, null=True, default=None)
    invoice_pdf = models.FileField(upload_to="invoices/", blank=True, null=True)

    coupon = models.ForeignKey("server.Coupon", on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))      # gross
    shipping_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))  # gross
    total_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))   # gross

    installments_months = models.PositiveIntegerField(null=True, blank=True)

    # Logged-in flow: FK to Address
    shipping_address = models.ForeignKey('server.Address', on_delete=models.SET_NULL, null=True, blank=True)

    # Snapshot ALWAYS stored, incl. guests:
    ship_first_name = models.CharField(max_length=100, blank=True, default="")
    ship_last_name  = models.CharField(max_length=100, blank=True, default="")
    ship_line1      = models.CharField(max_length=255, blank=True, default="")
    ship_line2      = models.CharField(max_length=255, blank=True, default="")
    ship_postal     = models.CharField(max_length=32, blank=True, default="")
    ship_city       = models.CharField(max_length=100, blank=True, default="")
    ship_country    = models.CharField(max_length=2, blank=True, default="")  # "DE", "FR", ...
    ship_phone      = models.CharField(max_length=64, blank=True, default="")
    ship_street_name  = models.CharField(max_length=255, blank=True, default="")
    ship_house_number = models.CharField(max_length=64, blank=True, default="")

    number = models.CharField(max_length=32, unique=True, null=True, blank=True, default=None)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    stripe_pi = models.CharField(max_length=255, blank=True, null=True)
    stripe_si = models.CharField(max_length=255, blank=True, null=True)

    stock_locked = models.BooleanField(default=False)
    stock_released = models.BooleanField(default=False)

    lookup_token = models.CharField(max_length=36, blank=True, default="")
    confirmation_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["invoice_number"],
                condition=Q(invoice_number__isnull=False) & ~Q(invoice_number=""),
                name="uniq_invoice_number_nonempty",
            ),
        ]

    def __str__(self):
        return f"Order #{self.pk}"
    
    def _iter_items(self):
        # Prefer related manager
        manager = getattr(self, "items", None)
        if manager is not None:
            yielded = False
            for it in manager.all().select_related("product"):
                yielded = True
                yield it
            if yielded:
                return
        # Fallback to query
        yielded2 = False
        for it in OrderItem.objects.filter(order=self).select_related("product"):
            yielded2 = True
            yield it
        if yielded2:
            return
        # Legacy single-line conversion (read-only)
        if self.product_id and self.quantity:
            yield OrderItem(
                order=self,
                product=self.product,
                quantity=self.quantity,
                unit_price=getattr(self.product, "final_price_gross", getattr(self.product, "price_gross", Decimal("0.00"))),
            )

    @property
    def ordered_at(self):
        return self.created_at

    def generate_invoice_number(self):
        from datetime import datetime
        return f"MM-{datetime.now():%m-%Y}-{self.pk:06d}"

    def assign_unique_invoice_number(self):
        """Set a unique invoice_number if empty or duplicated (adds -2, -3, … if needed)."""
        if not self.invoice_number:
            base = self.generate_invoice_number()
        else:
            base = self.invoice_number
        candidate = base
        i = 1
        from server.models import Order as O
        while O.objects.filter(invoice_number=candidate).exclude(pk=self.pk).exists():
            i += 1
            candidate = f"{base}-{i}"
        if candidate != self.invoice_number:
            self.invoice_number = candidate
            self.save(update_fields=["invoice_number"])

    def ensure_number(self):
        if not self.number:
            self.number = f"ORD-{self.created_at:%Y%m%d}-{self.pk:06d}"
            super().save(update_fields=["number"])

    def save(self, *args, **kwargs):
        creating = self.pk is None
        super().save(*args, **kwargs)
        if creating and not self.lookup_token:
            self.lookup_token = str(uuid.uuid4())
            super().save(update_fields=["lookup_token"])

    @property
    def payment_method_label(self):
        return self.get_payment_method_display()

    @property
    def return_deadline(self):
        if not self.delivered_at:
            return None
        # 15 days return window
        return self.delivered_at + timedelta(days=15)

    @property
    def is_returnable(self) -> bool:
        if self.status != "delivered" or not self.delivered_at:
            return False
        return timezone.now() <= (self.delivered_at + timedelta(days=15))

    @transaction.atomic
    def reserve_stock(self):
        """Decrease stock for all items after payment succeeds."""
        if self.stock_locked:
            return
        for it in self._iter_items():
            p = Product.objects.select_for_update().get(pk=it.product_id)
            if p.quantity < it.quantity:
                raise ValueError(f"Insufficient stock for {p} (have {p.quantity}, need {it.quantity})")
            p.quantity = F("quantity") - it.quantity
            p.save(update_fields=["quantity"])
        self.stock_locked = True
        self.save(update_fields=["stock_locked"])

    @transaction.atomic
    def release_stock(self):
        """Add back Product.quantity for all items if not already released."""
        if not self.stock_locked or self.stock_released:
            return
        for it in self._iter_items():
            p = Product.objects.select_for_update().get(pk=it.product_id)
            p.quantity = F("quantity") + it.quantity
            p.save(update_fields=["quantity"])
        self.stock_released = True
        self.save(update_fields=["stock_released"])

class OrderItem(models.Model):
    order = models.ForeignKey(Order, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="order_items")
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)  # gross

    def line_total(self):
        return _q2(Decimal(self.unit_price) * self.quantity)

    @property
    def product_name(self):
        try:
            return getattr(self.product, "title_en", None) or getattr(self.product, "title", None) or str(self.product)
        except Exception:
            return "Product"

    def __str__(self):
        return f"{self.product_name} x {self.quantity}"
    
# ----------------- Coupons & Promotions -----------------

DISCOUNT_TYPES = [("percent", "Percent"), ("fixed", "Fixed amount")]

class Coupon(models.Model):
    code = models.CharField(max_length=32, unique=True, db_index=True)
    kind = models.CharField(max_length=10, choices=DISCOUNT_TYPES, default="percent")
    value = models.DecimalField(max_digits=10, decimal_places=2)  # percent (e.g. 10) or fixed EUR
    active = models.BooleanField(default=True)

    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at   = models.DateTimeField(null=True, blank=True)

    min_subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    max_uses = models.PositiveIntegerField(null=True, blank=True)          # across everyone
    max_uses_per_user = models.PositiveIntegerField(null=True, blank=True)

    # target filters (leave both empty = applies to all)
    products = models.ManyToManyField("Product", blank=True, related_name="coupons")
    categories = ArrayField(models.CharField(max_length=30), default=list, blank=True)  # store category keys

    # optional cap for percent coupons
    max_discount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "code"]

    def __str__(self):
        return self.code.upper()

    def is_time_ok(self, now=None):
        now = now or timezone.now()
        if self.starts_at and now < self.starts_at: return False
        if self.ends_at and now > self.ends_at:     return False
        return True

    def eligible_items_subtotal(self, items):
        """
        items: list of dicts with product_id & qty OR OrderItem instances.
        Returns gross subtotal of eligible items.
        """
        subtotal = Decimal("0.00")
        allowed_products = set(self.products.values_list("id", flat=True)) if self.pk else set()
        allowed_categories = set((self.categories or []))

        for it in items:
            if hasattr(it, "product_id"):
                pid = it.product_id
                qty = it.quantity
                unit = it.unit_price  # already gross
                cat = it.product.category
            else:
                pid = int(it.get("product_id"))
                qty = int(it.get("qty"))
                p = Product.objects.get(pk=pid)
                unit = p.final_price_gross
                cat = p.category

            ok = True
            if allowed_products:
                ok = pid in allowed_products
            if ok and allowed_categories:
                ok = cat in allowed_categories

            if ok:
                subtotal += (Decimal(unit) * qty)

        return _q2(subtotal)

    def can_apply(self, user, items, cart_subtotal):
        """
        Return (ok, reason). Usage limits are checked against PAID redemptions.
        """
        if not self.active:
            return False, "Coupon is not active."
        if not self.is_time_ok():
            return False, "Coupon is not currently valid."
        if Decimal(cart_subtotal) < self.min_subtotal:
            return False, f"Minimum order amount is {self.min_subtotal:.2f} €."

        paid_q = CouponRedemption.objects.filter(coupon=self, order__status="paid")
        if self.max_uses is not None and paid_q.count() >= self.max_uses:
            return False, "Coupon usage limit reached."
        if user and self.max_uses_per_user is not None:
            user_paid = paid_q.filter(user=user).count()
            has_open = Order.objects.filter(
                user=user, coupon=self
            ).exclude(status__in=["cancelled", "failed"]).exists()
            if user_paid >= self.max_uses_per_user or has_open:
                return False, "You have already used this coupon."

        elig = self.eligible_items_subtotal(items)
        if (self.products.exists() or (self.categories or [])) and elig <= 0:
            return False, "Coupon does not apply to selected products."

        return True, ""

    def discount_amount(self, user, items, cart_subtotal):
        """Compute discount in EUR (gross)."""
        elig_subtotal = self.eligible_items_subtotal(items)
        base = elig_subtotal if (self.products.exists() or (self.categories or [])) else Decimal(cart_subtotal)
        if base <= 0:
            return Decimal("0.00")
        if self.kind == "percent":
            disc = (base * (self.value / Decimal("100")))
        else:
            disc = Decimal(self.value)
        if self.max_discount:
            disc = min(disc, self.max_discount)
        disc = min(disc, base)
        return _q2(disc)

class CouponRedemption(models.Model):
    coupon = models.ForeignKey(Coupon, on_delete=models.CASCADE, related_name="redemptions")
    user   = models.ForeignKey(User, on_delete=models.CASCADE, related_name="coupon_redemptions", null=True, blank=True)
    order  = models.OneToOneField("Order", on_delete=models.CASCADE, related_name="coupon_redemption")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = (("coupon", "order"),)
        indexes = [models.Index(fields=["coupon", "user"])]

# ----------------- Promotions -----------------

class PromotionBanner(models.Model):
    name = models.CharField(max_length=64)
    coupon = models.ForeignKey("Coupon", null=True, blank=True, on_delete=models.SET_NULL, related_name="promotions")
    code = models.CharField(max_length=32, blank=True, help_text="Optional manual code (used if coupon FK empty)")

    headline = models.CharField(max_length=120)
    subheadline = models.CharField(max_length=180, blank=True)
    start_at = models.DateTimeField()
    end_at   = models.DateTimeField()
    is_enabled = models.BooleanField(default=True)

    # Styling / placement
    bg_color = models.CharField(max_length=16, default="#111827")
    text_color = models.CharField(max_length=16, default="#ffffff")
    link_url = models.URLField(blank=True)
    image = models.ImageField(upload_to="promos/", null=True, blank=True)

    # Target markets (empty = all)
    country_whitelist = ArrayField(models.CharField(max_length=2), default=list, blank=True)
    priority = models.IntegerField(default=0)  # higher wins when overlapping

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-priority", "-updated_at"]

    def __str__(self):
        return self.name

    def current_code(self) -> str:
        return (self.coupon.code if self.coupon_id else (self.code or "")).upper()

    def active_now(self, now=None) -> bool:
        now = now or timezone.now()
        return self.is_enabled and self.start_at <= now <= self.end_at

# ----------------- Returns -----------------

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

# ----------------- Invoices -----------------

class Invoice(models.Model):
    order = models.OneToOneField("Order", on_delete=models.CASCADE, related_name="invoice")
    number = models.CharField(max_length=32, unique=True)
    pdf = models.FileField(upload_to="invoices/")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.number

# ----------------- Reviews -----------------

class Review(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reviews")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="reviews")
    rating = models.PositiveIntegerField()
    message = models.TextField()
    image = models.ImageField(upload_to="review_images/", blank=True, null=True)
    author_name = models.CharField(max_length=100, blank=True, default="")
    author_avatar = models.ImageField(upload_to="review_avatars/", blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} on {self.product} ({self.rating})"