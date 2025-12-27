#server/models.py
from typing import Any, Optional, List, Iterable, cast
from types import SimpleNamespace
from django.db.models import Q, UniqueConstraint, F, CheckConstraint
from django.contrib.auth.models import AbstractUser,  UserManager as DjangoUserManager
from django.contrib.postgres.fields import ArrayField
from django.db import models, transaction
from django.db.models.functions import Lower
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator, MinValueValidator, MaxValueValidator
from django.utils import timezone
from django.conf import settings
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import timedelta, datetime
from server.utils.images import resize_image_file
import secrets
import uuid
from django.contrib.auth.hashers import make_password, check_password

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

class CustomUserManager(DjangoUserManager["User"]):
    use_in_migrations = True

    def _create_user(self, email: str, password: Optional[str], **extra_fields: Any) -> "User":
        if not email:
            raise ValueError("The Email must be set")
        email = self.normalize_email(email)
        user = cast("User", self.model(email=email, **extra_fields))
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(
        self,                       
        email: Optional[str] = None,
        password: Optional[str] = None,
        **extra_fields: Any,
    ) -> "User":
        # for old code that might pass username instead of email
        if email is None:
            email = extra_fields.pop("username", None)

        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(
        self,
        email: Optional[str] = None,
        password: Optional[str] = None,
        **extra_fields: Any,
    ) -> "User":
        # for old code that might pass username instead of email
        if email is None:
            email = extra_fields.pop("username", None)

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

    def __str__(self) -> str:
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

class LoginOTP(models.Model):
    """
    Stores short-lived OTP for 2-step login.
    We store HASH (not plain code).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="login_otps")

    code_hash = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    attempts = models.PositiveIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)

    # optional: client metadata
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["user", "expires_at"]),
            models.Index(fields=["expires_at"]),
        ]

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    def is_consumed(self) -> bool:
        return self.consumed_at is not None

    def set_code(self, code: str) -> None:
        self.code_hash = make_password(code)

    def check_code(self, code: str) -> bool:
        return check_password(code, self.code_hash)

    @classmethod
    def create_for_user(cls, user, code: str, ip: str = "", user_agent: str = "", ttl_minutes: int = 10):
        now = timezone.now()
        obj = cls(
            user=user,
            expires_at=now + timedelta(minutes=ttl_minutes),
            ip=ip or None,
            user_agent=(user_agent or "")[:255],
        )
        obj.set_code(code)
        obj.save()
        return obj

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

        uid: Optional[int] = getattr(self, "user_id", None)  # appease type checker
        if uid and self.is_default and not self._state.adding:
            with transaction.atomic():
                (Address.objects
                    .select_for_update()
                    .filter(user_id=uid, is_default=True)
                    .exclude(pk=self.pk)
                    .update(is_default=False)
                )
        super().save(*args, **kwargs)

    def set_default_for_user(self):
        uid: Optional[int] = getattr(self, "user_id", None)
        if not uid:
            return
        with transaction.atomic():
            (Address.objects
                .select_for_update()
                .filter(user_id=uid, is_default=True)
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

    def __str__(self) -> str:
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
    carepara_en = models.TextField(blank=True, null=True)
    carepara_de = models.TextField(blank=True, null=True)

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
        elif self.price_gross is not None and self.price_net is None:
            gross = Decimal(self.price_gross)
            denom = (Decimal(1) + Decimal(self.vat_rate))
            if denom > 0:
                self.price_net = _q2(gross / denom)
        # ✅ Resize main image (e.g. 800×800) before saving
        if self.image and hasattr(self.image, "file"):
            resized = resize_image_file(
                self.image.file,
                max_width=800,
                max_height=800,
                output_format="WEBP",
                quality=85,
            )
            if resized is not None:
                self.image.save(resized.name, resized, save=False)

        creating = self.pk is None
        super().save(*args, **kwargs)

        # Assign pretty key after we have an ID
        if (creating or not self.key) and self.pk:
            cat_prefix = CATEGORY_CODES.get(self.category, "MIND")
            new_key = f"{cat_prefix}{str(cast(int, self.pk)).zfill(2)}"
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

        # ✅ Resize gallery image too (maybe smaller, e.g. 800 px)
        if self.media_type == "image" and self.image and hasattr(self.image, "file"):
            resized = resize_image_file(
                self.image.file,
                max_width=800,
                max_height=800,
                output_format="WEBP",
                quality=85,
            )
            if resized is not None:
                self.image.save(resized.name, resized, save=False)

        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.media_type.capitalize()} for {self.product}"

#---------------------------Admin Product Monitor----------------------------
class ProductEvent(models.Model):
    class Action(models.TextChoices):
        VIEW = "view", "View"
        ADD_TO_CART = "add_to_cart", "Add to cart"
        WISHLIST = "wishlist", "Wishlist"

    product = models.ForeignKey("Product", related_name="events", on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=32, choices=Action.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["product", "action", "created_at"]),
            models.Index(fields=["created_at"]),
        ]

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

    def __str__(self) -> str:
        return f"On-demand request #{self.pk} for {self.product} x{self.quantity}"


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

#------------------------Cart ------------------------------

class Cart(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey("server.User", null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="carts")
    cart_token = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    currency = models.CharField(max_length=3, default="EUR")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "is_active"]),
            models.Index(fields=["cart_token", "is_active"]),
        ]

    def __str__(self) -> str:
        # Type-narrow on user (Optional[User]) — Pylance understands this
        if self.user is not None:
            # getattr keeps it safe even if custom User lacks 'email' in stubs
            email: Optional[str] = getattr(self.user, "email", None)
            return f"Cart({email or 'user'})"
        tok = self.cart_token or "no-token"
        return f"Cart(guest:{tok})"


class CartItem(models.Model):
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("server.Product", on_delete=models.PROTECT)
    name = models.CharField(max_length=255)      # denormalized for admin clarity
    image = models.URLField(blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)  # EUR
    qty = models.PositiveIntegerField(default=1)
    color = models.CharField(max_length=64, blank=True)
    weight = models.CharField(max_length=64, blank=True)
    dimensions = models.CharField(max_length=128, blank=True)
    category = models.CharField(max_length=128, blank=True)

    class Meta:
        unique_together = [("cart", "product")]  # one line per product

    @property
    def line_total(self):
        return self.qty * self.price

# ----------------- Orders, Items, Returns -----------------

class Order(models.Model):
    PAYMENT_METHODS = [
        ("card", "Card"),
        ("card_installments", "Card Installments"),
        ("sepa_debit", "SEPA Debit"),
        ("sepa_installments", "SEPA Installments"),        
        ("paypal", "PayPal"),
        ("giropay", "Giropay"),
        ("klarna_single", "Klarna (single payment)"),
        ("klarna_installments", "Klarna (installments)"),
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

    def __str__(self) -> str:
        return f"Order #{self.pk}"

    # ---- numbering & totals ----

    def _generate_admin_number(self) -> str:
        # MM#### using DB pk to avoid collisions
        return f"MM2026{self.pk:04d}"

    def ensure_number(self):
        if not self.number and self.pk:
            self.number = self._generate_admin_number()
            super().save(update_fields=["number"])
        return self.number

    def assign_unique_order_number(self):
        """
        Backwards-compatible alias used by legacy signal hooks.
        """
        return self.ensure_number()

    def line_total(self) -> Decimal:
        total = Decimal("0.00")
        items_mgr = getattr(self, "items", None)  # reverse manager exists at runtime
        if not items_mgr:
            return _q2(total)
        for it in cast(Iterable[Any], items_mgr.all()):
            total += Decimal(getattr(it, "unit_price", Decimal("0.00"))) * int(getattr(it, "quantity", 0))
        return _q2(total)

    def compute_totals(
        self,
        shipping_price: Optional[Decimal] = None,
        vat_rate: Decimal = DEFAULT_VAT_RATE,
    ) -> Decimal:
        """
        Recalculate subtotal/discount/vat/total based on items and any attached coupon.
        Store results on the model (does not save).
        """
        items_mgr = getattr(self, "items", None)
        if items_mgr:
            items_qs: List[Any] = list(items_mgr.select_related("product"))
        else:
            items_qs = []

        subtotal = Decimal("0.00")
        for it in items_qs:
            unit = Decimal(getattr(it, "unit_price", Decimal("0.00")))
            qty = int(getattr(it, "quantity", 0))
            subtotal += (unit * qty)
        subtotal = _q2(subtotal)

        discount = Decimal("0.00")
        try:
            red = getattr(self, "coupon_redemption", None)  # OneToOne; may not exist
            if red is not None:
                coupon = getattr(red, "coupon", None)
                if coupon is not None:
                    # coupon.discount_amount(user, items, cart_subtotal)
                    discount = coupon.discount_amount(self.user, items_qs, subtotal)
        except Exception:
            discount = Decimal("0.00")

        ship = _q2(Decimal(shipping_price) if shipping_price is not None else Decimal(self.shipping_price or 0))

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
            items_mgr = getattr(self, "items", None)
            if not items_mgr:
                return
            items: List[Any] = list(items_mgr.all())
            prod_ids: List[int] = [
                int(pid) for pid in (getattr(it, "product_id", None) for it in items) if pid
            ]
            if not prod_ids:
                return
            products = {
                p.pk: p
                for p in Product.objects.select_for_update().filter(pk__in=prod_ids)
            }

            # validate
            for it in items:
                pid = getattr(it, "product_id", None)
                if not pid:
                    continue
                p = products[int(pid)]
                if int(getattr(p, "quantity", 0) or 0) < int(getattr(it, "quantity", 0)):
                    raise ValidationError(f"Insufficient stock for {p}")

            # decrement
            for it in items:
                pid = getattr(it, "product_id", None)
                if not pid:
                    continue
                Product.objects.filter(pk=int(pid)).update(
                    quantity=F("quantity") - int(getattr(it, "quantity", 0))
                )            


    def release_stock_atomic(self) -> None:
        with transaction.atomic():
            items_mgr = getattr(self, "items", None)
            if not items_mgr:
                return
            items: List[Any] = list(items_mgr.all())
            prod_ids: List[int] = [
                int(pid) for pid in (getattr(it, "product_id", None) for it in items) if pid
            ]
            if not prod_ids:
                return

            # Lock product rows
            Product.objects.select_for_update().filter(pk__in=prod_ids)

            # increment
            for it in items:
                pid = getattr(it, "product_id", None)
                if not pid:
                    continue
                Product.objects.filter(pk=int(pid)).update(
                    quantity=F("quantity") + int(getattr(it, "quantity", 0))
                )
    
    def lock_stock(self) -> None:
        """
        Reserve stock for all items on this order (idempotent).
        """
        if self.stock_locked:
            return

        with transaction.atomic():
            items_mgr = getattr(self, "items", None)
            if not items_mgr:
                return
            items: List[Any] = list(items_mgr.all())
            prod_ids: List[int] = [
                int(pid) for pid in (getattr(it, "product_id", None) for it in items) if pid
            ]
            products: dict[int, Product] = {}
            if prod_ids:
                products = {
                    p.pk: p
                    for p in Product.objects.select_for_update().filter(pk__in=prod_ids)
                }

                # validate
                for it in items:
                    pid = getattr(it, "product_id", None)
                    if not pid:
                        continue
                    p = products[int(pid)]
                    if int(getattr(p, "quantity", 0) or 0) < int(getattr(it, "quantity", 0)):
                        raise ValueError(
                            f"Insufficient stock for {p} (have {p.quantity}, need {getattr(it, 'quantity', 0)})"
                        )

                # decrement
                for it in items:
                    pid = getattr(it, "product_id", None)
                    if not pid:
                        continue
                    Product.objects.filter(pk=int(pid)).update(
                        quantity=F("quantity") - int(getattr(it, "quantity", 0))
                    )

            self.stock_locked = True
            self.save(update_fields=["stock_locked"])

    def release_stock(self) -> None:
        """
        Put reserved stock back (used on cancellation).
        """
        if not self.stock_locked:
            return

        with transaction.atomic():
            items_mgr = getattr(self, "items", None)
            if not items_mgr:
                return
            items: List[Any] = list(items_mgr.all())
            prod_ids: List[int] = [
                int(pid) for pid in (getattr(it, "product_id", None) for it in items) if pid
            ]

            if prod_ids:
                # lock product rows
                Product.objects.select_for_update().filter(pk__in=prod_ids)
                # increment
                for it in items:
                    pid = getattr(it, "product_id", None)
                    if not pid:
                        continue
                    Product.objects.filter(pk=int(pid)).update(
                        quantity=F("quantity") + int(getattr(it, "quantity", 0))
                    )

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
    def payment_method_label(self) -> str:
        disp = getattr(self, "get_payment_method_display", None)
        if callable(disp):
            return str(disp())
        return str(self.payment_method or "")

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
         return (
            self.status == "delivered"
            and self.delivered_at is not None
            and timezone.now() <= (self.delivered_at + timedelta(days=15))
        )
        
    @property
    def shipping_address(self):
        """
        Back-compat shim for code that expects order.shipping_address.<field>.
        Maps to the snapshot fields stored on Order.
        """
        return SimpleNamespace(
            first_name=self.ship_first_name or "",
            last_name=self.ship_last_name or "",
            line1=self.ship_line1 or "",
            line2=self.ship_line2 or "",
            postal_code=self.ship_postal or "",
            city=self.ship_city or "",
            country=self.ship_country or "",
            phone=self.ship_phone or "",
            street_name=getattr(self, "ship_street_name", "") or "",
            house_number=getattr(self, "ship_house_number", "") or "",
            full_name=(" ".join([self.ship_first_name, self.ship_last_name]).strip()),
        )

    @property
    def billing_address(self):
        """
        If you don't store a separate billing snapshot, reuse shipping.
        Update later if you add dedicated billing fields.
        """
        return self.shipping_address


class OrderItem(models.Model):
    order = models.ForeignKey(Order, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey("server.Product", null=True, blank=True, on_delete=models.SET_NULL)

    # Snapshot name at the time of ordering (avoid future title changes)
    product_name_snapshot = models.CharField(max_length=255)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    def line_total(self) -> Decimal:
        return _q2(Decimal(self.unit_price or 0) * int(self.quantity or 0))

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


DISCOUNT_TYPES = [("percent", "Percent"), ("fixed", "Fixed amount")]

COUPON_TARGETS = [
    ("public", "Public (anyone)"),
    ("users", "Assigned users only"),
    ("emails", "Allowed emails only"),
]


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

    # ✅ NEW: targeting mode
    target = models.CharField(max_length=10, choices=COUPON_TARGETS, default="public")

    # ✅ NEW: allow-list for specific users (requires login)
    assigned_users = models.ManyToManyField(
        "User",
        blank=True,
        related_name="assigned_coupons",
        help_text="If target=users, only these users can use the coupon.",
    )

    # ✅ NEW: allow-list for emails (works for guest checkout too, if you pass email)
    allowed_emails = ArrayField(
        models.EmailField(),
        default=list,
        blank=True,
        help_text="If target=emails, only these emails can use the coupon (case-insensitive).",
    )

    # targeting (existing)
    products = models.ManyToManyField("Product", blank=True, related_name="coupons")
    categories = ArrayField(models.CharField(max_length=30), default=list, blank=True)  # store category keys

    max_discount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
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

    def _email_ok(self, email: str) -> bool:
        email = (email or "").strip().lower()
        if not email:
            return False
        allowed = [e.strip().lower() for e in (self.allowed_emails or []) if str(e).strip()]
        return email in set(allowed)

    def _user_ok(self, user) -> bool:
        if not user or not getattr(user, "is_authenticated", False):
            return False
        # If assigned_users is empty, treat as "no restriction" only when target=public.
        return self.assigned_users.filter(pk=user.pk).exists()

    def eligible_items_subtotal(self, items):
        # (UNCHANGED) ...
        subtotal = Decimal("0.00")
        allowed_products = set(self.products.values_list("id", flat=True)) if self.pk else set()
        allowed_categories = set(self.categories or [])

        for it in items:
            if hasattr(it, "product_id"):
                pid = it.product_id
                qty = it.quantity
                unit = it.unit_price
                cat = it.product.category if getattr(it, "product", None) else None
            else:
                pid = int(it.get("product_id"))
                qty = int(it.get("qty", 1))
                p = Product.objects.get(pk=pid)
                unit = getattr(p, "final_price_gross", None) or p.price_gross
                unit = Decimal(str(unit))
                cat = p.category

            ok = True
            if allowed_products:
                ok = pid in allowed_products
            if ok and allowed_categories:
                ok = (cat in allowed_categories)

            if ok:
                subtotal += (Decimal(unit) * qty)

        return _q2(subtotal)

    def can_apply(self, user, items, cart_subtotal, email: str = ""):
        """
        Return (ok, reason).
        Usage limits are checked only against PAID redemptions.
        Targeting:
          - public: anyone
          - users: only assigned_users (requires login)
          - emails: only allowed_emails (guest or logged-in)
        """
        if not self.active:
            return False, "Coupon is not active."
        if not self.is_time_ok():
            return False, "Coupon is not currently valid."
        if Decimal(cart_subtotal) < self.min_subtotal:
            return False, f"Minimum order amount is {self.min_subtotal:.2f} €."

        # ✅ NEW: enforce targeting
        if self.target == "users":
            if not user or not getattr(user, "is_authenticated", False):
                return False, "Please sign in to use this coupon."
            if not self._user_ok(user):
                return False, "This coupon is not assigned to your account."
        elif self.target == "emails":
            # Prefer user.email if logged in; otherwise take provided email
            effective_email = ""
            if user and getattr(user, "is_authenticated", False):
                effective_email = (getattr(user, "email", "") or "").strip()
            effective_email = effective_email or (email or "").strip()
            if not self._email_ok(effective_email):
                return False, "This coupon is not available for this email."

        paid_q = CouponRedemption.objects.filter(coupon=self, order__status="paid")

        if self.max_uses is not None and paid_q.count() >= self.max_uses:
            return False, "Coupon usage limit reached."

        if user and getattr(user, "is_authenticated", False) and self.max_uses_per_user is not None:
            user_paid = paid_q.filter(user=user).count()
            has_open = Order.objects.filter(user=user, coupon_redemption__coupon=self) \
                                    .exclude(status__in=["cancelled", "failed"]).exists()
            if user_paid >= self.max_uses_per_user or has_open:
                return False, "You have already used this coupon."

        elig = self.eligible_items_subtotal(items)
        if (self.products.exists() or (self.categories or [])) and elig <= 0:
            return False, "Coupon does not apply to selected products."

        return True, ""

    def discount_amount(self, user, items, cart_subtotal):
        # (UNCHANGED) ...
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

    headline = models.CharField(max_length=1020)
    subheadline = models.CharField(max_length=1080, blank=True)
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
        if self.coupon:  # ForeignKey can be None
            return (self.coupon.code or "").upper()
        return (self.code or "").upper()

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


# ----------------- Digital Marketing & Tracking -----------------


class MarketingEvent(models.Model):
    """
    Generic tracking event for digital marketing / analytics.
    Can be sent from the frontend whenever something important happens:
    - page_view
    - product_view
    - add_to_cart
    - checkout_start
    - order_completed
    - newsletter_subscribe
    - coupon_applied
    etc.
    """

    class EventType(models.TextChoices):
        PAGE_VIEW = "page_view", "Page view"
        PRODUCT_VIEW = "product_view", "Product view"
        ADD_TO_CART = "add_to_cart", "Add to cart"
        CART_VIEW = "cart_view", "Cart view"
        CHECKOUT_START = "checkout_start", "Checkout started"
        ORDER_COMPLETED = "order_completed", "Order completed"
        NEWSLETTER_SUBSCRIBE = "newsletter_subscribe", "Newsletter subscribe"
        COUPON_APPLIED = "coupon_applied", "Coupon applied"
        WISHLIST_ADD = "wishlist_add", "Wishlist add"

    # Who did it (optional, because many visitors are guests)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="marketing_events",
    )

    # Anonymous/browser tracking
    session_id = models.CharField(
        max_length=64,
        blank=True,
        db_index=True,
        help_text="Anonymous session or client id from frontend (e.g. localStorage).",
    )

    event_type = models.CharField(max_length=64, choices=EventType.choices)

    # Optional relations to link events back to main objects
    product = models.ForeignKey(
        "Product",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="marketing_events",
    )
    order = models.ForeignKey(
        "Order",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="marketing_events",
    )
    coupon = models.ForeignKey(
        "Coupon",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="marketing_events",
    )

    # Where it happened
    url_path = models.CharField(
        max_length=512,
        blank=True,
        help_text="Path on the MindMines site (e.g. /products/marble-bowl).",
    )
    referrer = models.CharField(
        max_length=512,
        blank=True,
        help_text="Referrer URL if available.",
    )

    # UTM parameters for campaigns
    utm_source = models.CharField(max_length=64, blank=True)
    utm_medium = models.CharField(max_length=64, blank=True)
    utm_campaign = models.CharField(max_length=128, blank=True)
    utm_term = models.CharField(max_length=128, blank=True)
    utm_content = models.CharField(max_length=128, blank=True)

    # Basic geo/device info (optional, you can fill from backend or frontend)
    country = models.CharField(
        max_length=2,
        blank=True,
        help_text="ISO country code if known (e.g. DE).",
    )
    device_type = models.CharField(
        max_length=32,
        blank=True,
        help_text="desktop / mobile / tablet etc.",
    )
    user_agent = models.CharField(max_length=256, blank=True)

    # Free-form JSON for extra data (e.g. A/B test variant, scroll depth, etc.)
    extra = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["event_type", "created_at"]),
            models.Index(fields=["session_id", "created_at"]),
            models.Index(fields=["utm_source", "utm_medium", "utm_campaign"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.event_type} @ {self.created_at:%Y-%m-%d %H:%M}"


class AbandonedCartReminder(models.Model):
    """
    Log of 'abandoned cart' reminder emails / campaigns.
    Used so you don't spam the same cart/customer many times.
    """

    cart = models.ForeignKey(
        Cart,
        on_delete=models.CASCADE,
        related_name="abandoned_reminders",
    )
    # Normally the cart is linked to a user, but we keep an explicit email snapshot
    email = models.EmailField(blank=True)

    reminder_number = models.PositiveIntegerField(
        default=1,
        help_text="1 = first reminder, 2 = second, etc.",
    )
    campaign_name = models.CharField(
        max_length=128,
        blank=True,
        help_text="Internal name of the campaign (e.g. 'WINTER24_ABANDONED_CART').",
    )

    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("cart", "reminder_number")
        indexes = [
            models.Index(fields=["sent_at"]),
            models.Index(fields=["campaign_name"]),
        ]

    def __str__(self) -> str:
        return f"Abandoned cart reminder #{self.reminder_number} for cart {self.cart_id}"
