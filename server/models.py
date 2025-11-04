#server/models.py
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.contrib.postgres.fields import ArrayField
from django.db import models, transaction
from django.db.models import Q, UniqueConstraint, F, CheckConstraint
from django.db.models.functions import Lower
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator, MinValueValidator, MaxValueValidator
from django.utils import timezone
from django.conf import settings
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import timedelta, datetime
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
]

COLOR_CHOICES = [
    ("white", "White"),
    ("black", "Black"),
    ("green", "Green"),
    ("grey", "Grey"),
]

DEFAULT_VAT_RATE = Decimal("0.19")


def _q2(d: Decimal) -> Decimal:
    return Decimal(d).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ----------------- Users -----------------

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
    # username optional; email is the login identifier
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

    # Birthday coupon logic
    date_of_birth = models.DateField(null=True, blank=True, db_index=True,
                                     help_text="Used for birthday coupon emails")

    # Preferred language for emails/content
    language = models.CharField(max_length=5, default="en", help_text="Preferred language code ('en' or 'de').")

    def __str__(self):
        return f"{self.user.email} profile"



# ----------------- Addresses -----------------

class Address(models.Model):
    user = models.ForeignKey(
        "server.User",
        on_delete=models.CASCADE,
        related_name="addresses",
        null=True,
        blank=True,
    )

    # Checkout-friendly fields
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    address_line1 = models.CharField(max_length=255, blank=True)  # street + house no. (FIXED)
    apartment = models.CharField(max_length=100, blank=True)      # Address supplement
    postal_code = models.CharField(max_length=20, blank=True)
    city = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=20, blank=True)

    # Legacy snapshot fields (kept for backward compatibility)
    full_name = models.CharField(max_length=255, blank=True)
    house_number = models.CharField(max_length=50, blank=True)
    street_name = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=100, blank=True)
    pincode = models.CharField(max_length=20, blank=True)

    is_default = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["user"], condition=Q(is_default=True),
                             name="one_default_address_per_user"),
        ]
        indexes = [
            models.Index(fields=["user", "is_default"]),
        ]

    def save(self, *args, **kwargs):
        """
        Rules:
        - Only one default per user.
        - When creating with is_default=True, save as non-default, then atomically set default.
        """
        if self._state.adding and self.is_default:
            super().save(*args, **kwargs)
            self.set_default_for_user()
            return

        if self.user_id and self.is_default and not self._state.adding:
            with transaction.atomic():
                (Address.objects
                    .select_for_update()
                    .filter(user_id=self.user_id, is_default=True)
                    .exclude(pk=self.pk)
                    .update(is_default=False)
                )

        super().save(*args, **kwargs)

    def set_default_for_user(self):
        if not self.user_id:
            return
        with transaction.atomic():
            (Address.objects
                .select_for_update()
                .filter(user=self.user, is_default=True)
                .exclude(pk=self.pk)
                .update(is_default=False)
            )
            if not self.is_default:
                self.is_default = True
                super().save(update_fields=["is_default"])

    def __str__(self):
        user_email = getattr(self.user, "email", "")
        name = (
            " ".join(filter(None, [self.first_name, self.last_name]))
            or self.full_name
            or user_email
        )
        line1 = (
            self.address_line1
            or " ".join([self.street_name, self.house_number]).strip()
            or "Address"
        )
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
        # enforce max 10 gallery images per stone
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

class Product(models.Model):
    # Titles
    title_en = models.CharField(max_length=255, default="")
    title_de = models.CharField(max_length=255, default="")

    # 3 short bullet-style lines (USPs)
    short_description1_en = models.CharField(max_length=255, blank=True)
    short_description1_de = models.CharField(max_length=255, blank=True)
    short_description2_en = models.CharField(max_length=255, blank=True)
    short_description2_de = models.CharField(max_length=255, blank=True)
    short_description3_en = models.CharField(max_length=255, blank=True)
    short_description3_de = models.CharField(max_length=255, blank=True)

    # product description paragraphs
    descriptionpara1_en = models.TextField(blank=True)
    descriptionpara1_de = models.TextField(blank=True)
    descriptionpara2_en = models.TextField(blank=True)
    descriptionpara2_de = models.TextField(blank=True)
    descriptionpara3_en = models.TextField(blank=True)
    descriptionpara3_de = models.TextField(blank=True)

    # care / delivery / returns text content
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
    quantity = models.PositiveIntegerField(default=0, help_text="Authoritative stock count")

    # misc
    dimensions = models.CharField(max_length=255, blank=True)
    weight = models.CharField(max_length=100, blank=True)
    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES, default="home_decor")

    # pricing (gross is VAT-included)
    price_net = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    vat_rate = models.DecimalField(
        max_digits=4,
        decimal_places=4,
        default=DEFAULT_VAT_RATE,
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("1.00"))],
        help_text="VAT as a fraction (0.19 = 19%).",
    )
    price_gross = models.DecimalField(max_digits=10, decimal_places=2, help_text="Displayed price incl. VAT")
    sale_price_gross = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True, help_text="Optional discounted price incl. VAT")
    discount_percent = models.PositiveIntegerField(blank=True, null=True, help_text="e.g. 10 = 10%. Must be <100")

    key = models.CharField(max_length=20, unique=True, null=True, blank=True, help_text="Public product key (HOME01, SCULPT03, ...)")
    image = models.ImageField(upload_to="decor-products/", null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            CheckConstraint(check=Q(quantity__gte=0), name="product_quantity_nonnegative"),
        ]
        indexes = [
            models.Index(fields=["category"]),
            models.Index(fields=["created_at"]),
        ]

    def clean(self):
        # Validate sale price
        if self.sale_price_gross is not None:
            try:
                sale = Decimal(self.sale_price_gross)
                if sale <= 0:
                    self.sale_price_gross = None
            except (InvalidOperation, TypeError):
                self.sale_price_gross = None

        if self.sale_price_gross is not None and self.price_gross is not None:
            if Decimal(self.sale_price_gross) >= Decimal(self.price_gross):
                raise ValidationError({"sale_price_gross": "Sale price must be lower than base gross price."})

        # Validate discount range
        if self.discount_percent is not None and not (0 <= int(self.discount_percent) <= 99):
            raise ValidationError({"discount_percent": "Discount percent must be between 0 and 99."})

        # Guard VAT range
        if self.vat_rate is None:
            self.vat_rate = DEFAULT_VAT_RATE
        else:
            vr = Decimal(self.vat_rate)
            if vr < 0 or vr > 1:
                raise ValidationError({"vat_rate": "VAT must be between 0 and 1 (e.g., 0.19 for 19%)."})

    def save(self, *args, **kwargs):
        self.clean()

        # If merchant entered net, auto-calc gross
        if self.price_net is not None:
            net = Decimal(self.price_net)
            self.price_gross = _q2(net * (Decimal(1) + Decimal(self.vat_rate)))
        # If only gross entered, back-calc net for convenience
        elif self.price_gross is not None and self.price_net is None:
            gross = Decimal(self.price_gross)
            denom = (Decimal(1) + Decimal(self.vat_rate))
            if denom > 0:
                self.price_net = _q2(gross / denom)

        creating = self.pk is None
        super().save(*args, **kwargs)

        # Assign pretty key after we have an ID
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
        """
        What customer actually pays per unit (incl VAT), using:
        - sale_price_gross if valid
        - else discount_percent if set
        - else price_gross
        """
        base = Decimal(self.price_gross or 0)
        sale = (Decimal(self.sale_price_gross or 0) if self.sale_price_gross is not None else None)
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

    def clean(self):
        if self.media_type == "image" and not self.image:
            raise ValidationError({"image": "Image file is required for media_type=image"})
        if self.media_type == "video" and not self.video:
            raise ValidationError({"video": "Video file is required for media_type=video"})

    def save(self, *args, **kwargs):
        # enforce max 10 images and 10 videos per product
        if self.pk is None:
            if self.media_type == "image":
                cnt = ProductMedia.objects.filter(product=self.product, media_type="image").count()
                if cnt >= 10:
                    raise ValidationError("Each product can have at most 10 images.")
            if self.media_type == "video":
                cnt = ProductMedia.objects.filter(product=self.product, media_type="video").count()
                if cnt >= 10:
                    raise ValidationError("Each product can have at most 10 videos.")
        self.clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.media_type.capitalize()} for {self.product}"


# ----------------- Wishlist / On-demand / Stock alerts -----------------

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
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["product"]),
        ]


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
        constraints = [UniqueConstraint(Lower("email"), name="uniq_newsletter_email_ci")]

    def __str__(self):
        return self.email


# ----------------- Orders, Items, Returns -----------------

class Order(models.Model):
    PAYMENT_METHODS = [
        ("card", "Card"),
        ("card_installments", "Card installments"),
        ("sepa_debit", "SEPA Direct Debit"),
        ("sepa_installments", "SEPA Direct Debit Installments"),
        ("klarna", "Klarna (Stripe Klarna)"),
        ("paypal", "PayPal / PayPal Pay Later"),
            ]

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("processing", "Processing"),
        ("paid", "Paid"),
        ("failed", "Failed"),
        ("shipped", "Shipped"),
        ("delivered", "Delivered"),
        ("cancelled", "Cancelled"),
    ]

    PAYMENT_STATUS_CHOICES = [
        ("", "Unknown"),
        ("processing", "Processing"),
        ("paid", "Paid"),
        ("failed", "Failed"),
    ]

    id = models.AutoField(primary_key=True)
    number = models.CharField(max_length=32, unique=True, blank=True)
    lookup_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)  # secure guest links

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="orders"
    )
    email = models.EmailField()

    # snapshot shipping at checkout time
    ship_first_name = models.CharField(max_length=120, blank=True)
    ship_last_name = models.CharField(max_length=120, blank=True)
    ship_line1 = models.CharField(max_length=255, blank=True)
    ship_line2 = models.CharField(max_length=255, blank=True)
    ship_postal = models.CharField(max_length=50, blank=True)
    ship_city = models.CharField(max_length=120, blank=True)
    ship_country = models.CharField(max_length=2, default="DE")
    ship_phone = models.CharField(max_length=64, blank=True)
    ship_street_name  = models.CharField(max_length=255, blank=True, default="")
    ship_house_number = models.CharField(max_length=64,  blank=True, default="")

    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    discount_total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    shipping_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    vat_total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    total_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    payment_method = models.CharField(max_length=32, choices=PAYMENT_METHODS, blank=True)
    installments_months = models.PositiveIntegerField(null=True, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending", db_index=True)
    payment_status = models.CharField(max_length=30, choices=PAYMENT_STATUS_CHOICES, default="", db_index=True)
    payment_ref = models.CharField(max_length=128, blank=True)  # Stripe PI id, PayPal order id, schedule id, etc.
    payment_id = models.CharField(max_length=128, blank=True)   # final captured charge id etc.

    shipped_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    tracking_number = models.CharField(max_length=64, blank=True, default="")
    tracking_carrier = models.CharField(max_length=64, blank=True, default="")
    tracking_url = models.URLField(blank=True, default="")

    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # bookkeeping
    stripe_pi = models.CharField(max_length=128, blank=True)  # PaymentIntent id
    invoice_number = models.CharField(max_length=64, blank=True)
    confirmation_sent_at = models.DateTimeField(null=True, blank=True)
    invoice_sent_at = models.DateTimeField(null=True, blank=True)
    failed_email_sent_at = models.DateTimeField(null=True, blank=True)
    stock_locked = models.BooleanField(
        default=False,
        help_text="True once inventory for items is reserved."
    )
    stock_released = models.BooleanField(
        default=False,
        help_text="True when a previous reservation was returned to stock."
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["invoice_number"],
                condition=Q(invoice_number__isnull=False) & ~Q(invoice_number=""),
                name="uniq_invoice_number_nonempty",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["payment_status"]),
            models.Index(fields=["email"]),
        ]

    def __str__(self):
        return f"Order #{self.pk}"

    # ---- numbering & totals ----

    def _generate_admin_number(self) -> str:
        # MM######## using DB pk to avoid collisions
        return f"MM{self.pk:08d}"

    def ensure_number(self):
        if not self.number and self.pk:
            self.number = self._generate_admin_number()
            super().save(update_fields=["number"])
        return self.number

    def line_total(self) -> Decimal:
        total = Decimal("0.00")
        for it in self.items.all():
            total += it.line_total()
        return _q2(total)

    def compute_totals(self, shipping_price: Decimal = None, vat_rate: Decimal = DEFAULT_VAT_RATE):
        """
        Recalculate subtotal/discount/vat/total based on items and any attached coupon.
        Store results on the model (does not save).
        """
        items_qs = list(self.items.select_related("product"))
        # items subtotal uses each item's unit_price (already gross) * qty
        subtotal = Decimal("0.00")
        for it in items_qs:
            subtotal += it.line_total()
        subtotal = _q2(subtotal)

        # coupon discount (if present)
        discount = Decimal("0.00")
        try:
            if hasattr(self, "coupon_redemption") and self.coupon_redemption:
                coupon = self.coupon_redemption.coupon
                discount = coupon.discount_amount(self.user, items_qs, subtotal)
        except Exception:
            discount = Decimal("0.00")

        # shipping
        ship = _q2(Decimal(shipping_price) if shipping_price is not None else Decimal(self.shipping_price or 0))

        # VAT portion informative (gross pricing): vat = (gross / (1+rate)) * rate
        gross_after_discount = max(Decimal("0.00"), subtotal - discount)
        vat_total = _q2((gross_after_discount / (Decimal(1) + Decimal(vat_rate))) * Decimal(vat_rate))

        total = _q2(gross_after_discount + ship)

        self.subtotal = _q2(subtotal)
        self.discount_total = _q2(discount)
        self.shipping_price = ship
        self.vat_total = vat_total
        self.total_price = total
        return total

    # ---- stock helpers (to be called from service layer) ----

    def reserve_stock_atomic(self):
        """
        Decrement product.quantity for each item atomically with row locks.
        """
        with transaction.atomic():
            for it in self.items.select_for_update(of=("self",)).select_related("product"):
                if it.product and hasattr(it.product, "quantity"):
                    if (it.product.quantity or 0) < it.quantity:
                        raise ValidationError(f"Insufficient stock for {it.product}")
                    it.product.quantity = F("quantity") - it.quantity
                    it.product.save(update_fields=["quantity"])

    def release_stock_atomic(self):
        with transaction.atomic():
            for it in self.items.select_for_update(of=("self",)).select_related("product"):
                if it.product and hasattr(it.product, "quantity"):
                    it.product.quantity = F("quantity") + it.quantity
                    it.product.save(update_fields=["quantity"])
    
    def lock_stock(self) -> None:
        """
        Reserve stock for all items on this order (idempotent).
        """
        if self.stock_locked:
            return
        
        with transaction.atomic():
            # pessimistic lock products touched by this order
            item_qs = self.items.select_related("product").select_for_update()
            for it in item_qs:
                prod = it.product
                if not prod:
                    continue
                if (prod.quantity or 0) < it.quantity:
                    raise ValueError(f"Insufficient stock for {prod} (have {prod.quantity}, need {it.quantity})")
                type(prod).objects.filter(pk=prod.pk).update(quantity=F("quantity") - it.quantity)
            self.stock_locked = True
            self.save(update_fields=["stock_locked"])

    def release_stock(self) -> None:
        """
        Put reserved stock back (used on cancellation).
        """
        if not self.stock_locked:
            return
        
        with transaction.atomic():
            item_qs = self.items.select_related("product").select_for_update()
            for it in item_qs:
                prod = it.product
                if not prod:
                    continue
                type(prod).objects.filter(pk=prod.pk).update(quantity=F("quantity") + it.quantity)
            self.stock_locked = False
            self.save(update_fields=["stock_locked"])

    # ---- invoices ----

    def generate_invoice_number(self) -> str:
        # Example: MM-11-2025-000123
        return f"MM-{datetime.now():%m-%Y}-{self.pk:06d}"

    def assign_unique_invoice_number(self) -> str:
        base = self.invoice_number or self.generate_invoice_number()
        candidate = base
        i = 1
        while type(self).objects.filter(invoice_number=candidate).exclude(pk=self.pk).exists():
            i += 1
            candidate = f"{base}-{i}"
        if candidate != self.invoice_number:
            self.invoice_number = candidate
            super().save(update_fields=["invoice_number"])
        return self.invoice_number

    # ---- lifecycle ----

    def save(self, *args, **kwargs):
        creating = self.pk is None
        super().save(*args, **kwargs)
        if creating:
            # now we have a pk -> set MM######## number
            self.ensure_number()

    @property
    def payment_method_label(self):
        return self.get_payment_method_display()

    @property
    def ordered_at(self):
        return self.created_at

    @property
    def return_deadline(self):
        if not self.delivered_at:
            return None
        return self.delivered_at + timedelta(days=15)

    @property
    def is_returnable(self) -> bool:
        return self.status == "delivered" and self.delivered_at and timezone.now() <= (self.delivered_at + timedelta(days=15))


class OrderItem(models.Model):
    order = models.ForeignKey(Order, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey("server.Product", null=True, blank=True, on_delete=models.SET_NULL)

    # Snapshot name at the time of ordering (avoid future title changes)
    product_name_snapshot = models.CharField(max_length=255)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    def line_total(self):
        return _q2(Decimal(self.unit_price) * self.quantity)

    @property
    def product_name(self):
        if self.product_name_snapshot:
            return self.product_name_snapshot
        try:
            return (
                getattr(self.product, "title_en", None)
                or getattr(self.product, "title", None)
                or str(self.product)
            )
        except Exception:
            return "Product"

    def __str__(self):
        return f"{self.product_name} x {self.quantity}"


# ----------------- Coupons & Promotions -----------------

DISCOUNT_TYPES = [("percent", "Percent"), ("fixed", "Fixed amount")]


class Coupon(models.Model):
    code = models.CharField(max_length=32, unique=True, db_index=True)
    kind = models.CharField(max_length=10, choices=DISCOUNT_TYPES, default="percent")
    value = models.DecimalField(max_digits=10, decimal_places=2, help_text="Percent (e.g. 10) or fixed EUR amount")
    active = models.BooleanField(default=True)

    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)

    min_subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    max_uses = models.PositiveIntegerField(null=True, blank=True, help_text="Global usage limit across everyone")
    max_uses_per_user = models.PositiveIntegerField(null=True, blank=True, help_text="Per-account usage limit")

    # targeting
    products = models.ManyToManyField("Product", blank=True, related_name="coupons")
    categories = ArrayField(models.CharField(max_length=30), default=list, blank=True)  # store category keys

    # optional cap for percent coupons
    max_discount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    # birthday flag (for admin campaigns)
    is_birthday = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "code"]

    def __str__(self):
        return self.code.upper()

    def is_time_ok(self, now=None):
        now = now or timezone.now()
        if self.starts_at and now < self.starts_at:
            return False
        if self.ends_at and now > self.ends_at:
            return False
        return True

    def eligible_items_subtotal(self, items):
        """
        items: iterable of OrderItem OR dicts { "product_id": int, "qty": int }
        Returns gross subtotal of eligible items only.
        """
        subtotal = Decimal("0.00")
        allowed_products = set(self.products.values_list("id", flat=True)) if self.pk else set()
        allowed_categories = set(self.categories or [])

        for it in items:
            # normalize access
            if hasattr(it, "product_id"):
                pid = it.product_id
                qty = it.quantity
                unit = it.unit_price  # already gross
                cat = it.product.category if it.product else None
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
                ok = (cat in allowed_categories)

            if ok:
                subtotal += (Decimal(unit) * qty)

        return _q2(subtotal)

    def can_apply(self, user, items, cart_subtotal):
        """
        Return (ok, reason). Usage limits are checked only against PAID redemptions.
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
            has_open = Order.objects.filter(user=user, couponredemption__coupon=self).exclude(status__in=["cancelled", "failed"]).exists()
            if user_paid >= self.max_uses_per_user or has_open:
                return False, "You have already used this coupon."

        elig = self.eligible_items_subtotal(items)
        if (self.products.exists() or (self.categories or [])) and elig <= 0:
            return False, "Coupon does not apply to selected products."

        return True, ""

    def discount_amount(self, user, items, cart_subtotal):
        """
        Compute discount in EUR (gross).
        """
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
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="coupon_redemptions", null=True, blank=True)
    order = models.OneToOneField("Order", on_delete=models.CASCADE, related_name="coupon_redemption")
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
    end_at = models.DateTimeField()
    is_enabled = models.BooleanField(default=True)

    # Styling / placement
    bg_color = models.CharField(max_length=16, default="#111827")
    text_color = models.CharField(max_length=16, default="#ffffff")
    link_url = models.URLField(blank=True)
    image = models.ImageField(upload_to="promos/", null=True, blank=True)

    # Target markets (empty = all)
    country_whitelist = ArrayField(models.CharField(max_length=2), default=list, blank=True)

    priority = models.IntegerField(default=0, help_text="Higher wins when multiple promos overlap")

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
    REQUEST_TYPES = (("cancel", "Cancel"), ("return", "Return"))
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
    rating = models.PositiveIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    message = models.TextField(blank=True, default="")
    image = models.ImageField(upload_to="review_images/", blank=True, null=True)
    author_name = models.CharField(max_length=100, blank=True, default="")
    author_avatar = models.ImageField(upload_to="review_avatars/", blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} on {self.product} ({self.rating})"


# ----------------- Birthday Coupons -----------------

class BirthdayCouponLog(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="birthday_coupons")
    sent_on = models.DateField(help_text="Store the YYYY-MM-DD we sent the birthday coupon")
    coupon_code = models.CharField(max_length=32, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "sent_on")
        indexes = [
            models.Index(fields=["sent_on"]),
            models.Index(fields=["user", "sent_on"]),
        ]

    def __str__(self):
        return f"Birthday coupon for {self.user.email} on {self.sent_on}"
