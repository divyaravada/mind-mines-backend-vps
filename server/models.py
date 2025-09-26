
from django.contrib.auth.models import AbstractUser,BaseUserManager
from django.db import models
from decimal import Decimal, ROUND_HALF_UP
from django.db.models import Q, UniqueConstraint
from django.db.models.functions import Lower
from django.core.exceptions import ValidationError
from datetime import timedelta
from django.utils import timezone
from django.conf import settings


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
    # keep an optional username field so admin can fill it if desired
    username = models.CharField(max_length=100, blank=True, null=True)
    email = models.EmailField(unique=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []  # createsuperuser will only ask for email + password

    objects = CustomUserManager()  # <-- IMPORTANT

    def __str__(self):
        return self.email

class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    full_name = models.CharField(max_length=255, blank=True)
    mobile = models.CharField(max_length=20, blank=True)
    image = models.ImageField(upload_to="profile_pics/", blank=True, null=True)
    verified = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.user.email} profile"

class Address(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='addresses')

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

    def set_default_for_user(self):
        Address.objects.filter(user=self.user, is_default=True).exclude(pk=self.pk).update(is_default=False)
        if not self.is_default:
            self.is_default = True
            self.save(update_fields=["is_default"])

    def __str__(self):
        name = " ".join(filter(None, [self.first_name, self.last_name])) or self.full_name or self.user.email
        line1 = self.address_line1 or " ".join([self.street_name, self.house_number]).strip() or "Address"
        return f"{name} — {line1}"
    
    
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

    image = models.ImageField(
        upload_to="natural-stone/images/",
        blank=True,
        null=True,
        help_text="Primary display image for the stone",
    )

    def __str__(self):
        return self.type

class NaturalStoneImage(models.Model):
    stone = models.ForeignKey(NaturalStone, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to="natural-stone/images/")

    def save(self, *args, **kwargs):
        # ✅ hard limit to 10 per stone
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

    
class Product(models.Model):
    title_en = models.CharField(max_length=255, default="Default Title")
    title_de = models.CharField(max_length=255, default="Default Title")


    short_description1_en = models.CharField(max_length=255, blank=True)
    short_description1_de = models.CharField(max_length=255, blank=True)
    short_description2_en = models.CharField(max_length=255, blank=True)
    short_description2_de = models.CharField(max_length=255, blank=True)
    short_description3_en = models.CharField(max_length=255, blank=True)
    short_description3_de = models.CharField(max_length=255, blank=True)

    descriptionpara1_en = models.TextField(blank=True)
    descriptionpara1_de = models.TextField(blank=True)
    descriptionpara2_en = models.TextField(blank=True)
    descriptionpara2_de = models.TextField(blank=True)
    descriptionpara3_en = models.TextField(blank=True)
    descriptionpara3_de = models.TextField(blank=True)

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

    type_of_stone = models.CharField(max_length=50, choices=STONE_TYPES)
    material = models.CharField(max_length=20, choices=MATERIAL_CHOICES, default="marble")
    color = models.CharField(max_length=20, choices=COLOR_CHOICES, default="white")

    stone_color_image = models.ImageField(upload_to="decor-products/colors/", null=True, blank=True)

    quantity = models.PositiveIntegerField(default=0)

    dimensions = models.CharField(max_length=255, blank=True)
    weight = models.CharField(max_length=100, blank=True)

    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES, default="home_decor")
    price = models.DecimalField(max_digits=8, decimal_places=2)

    key = models.CharField(max_length=20, unique=True, null=True, blank=True)

    image = models.ImageField(upload_to="decor-products/", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)  # if missing, add & migrate
    class Meta:
        ordering = ["-created_at", "id"]  # or just ["-id"]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.key:
            cat_prefix = CATEGORY_CODES.get(self.category, "MIND")
            self.key = f"{cat_prefix}{str(self.id).zfill(2)}"
            super().save(update_fields=["key"])

    def vat_included_price(self):
        vat_multiplier = Decimal("1.19")
        return (self.price * vat_multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def stock_message(self):
        if self.quantity == 0:
            return "Out of stock"
        elif self.quantity < 5:
            return "Hurry up! Only a few left"
        return "In stock"

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

    def __str__(self):
        return f"{self.media_type.capitalize()} for {self.product}"

class Wishlist(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)  # optional
    class Meta:
        ordering = ["-id"]
        unique_together = ("user", "product")


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


ORDER_STATUS = [
    ("pending", "Pending"),
    ("processing", "Processing"),
    ("paid", "Paid"),
    ("shipped", "Shipped"),
    ("delivered", "Delivered"),
    ("cancelled", "Cancelled"),
    ("failed", "Failed"),
]

PAYMENT_METHODS = [
    ("card", "Card"),
    ("paypal", "PayPal"),
    ("bank", "Bank Transfer"),
    ("klarna", "Klarna"),
]

class OrderItem(models.Model):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="order_items")
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)

    def line_total(self):
        return (self.unit_price * self.quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def product_name(self):
        # defensive: works even if the product title field differs
        try:
            return getattr(self.product, "title_en", None) or getattr(self.product, "title", None) or str(self.product)
        except Exception:
            return "Product"

    def __str__(self):
        return f"{self.product_name} x {self.quantity}"

class Order(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='orders')

    # legacy (optional)
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, blank=True)
    quantity = models.PositiveIntegerField(null=True, blank=True)

    # NEW
    items = models.ManyToManyField('OrderItem', blank=True, related_name='orders')
    payment_method = models.CharField(
        max_length=16,
        choices=[("paypal","PayPal"),("card","Card"),("klarna","Klarna"),("bank","Bank Transfer")],
        default="paypal",
    )
    status = models.CharField(
        max_length=32,
        choices=[("pending","Pending"),("paid","Paid"),("processing","Processing"),
                 ("shipped","Shipped"),("delivered","Delivered"),("cancelled","Cancelled")],
        default="pending",
    )
    payment_ref = models.CharField(max_length=128, blank=True, default="")
    payment_id = models.CharField(max_length=128, blank=True, default="")
    tracking_number = models.CharField(max_length=64, blank=True, default="")  # ← keep just once

    invoice_number = models.CharField(max_length=32, blank=True, null=True, default=None)
    invoice_pdf = models.FileField(upload_to="invoices/", blank=True, null=True)

    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    shipping_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    total_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    installments_months = models.PositiveIntegerField(null=True, blank=True)  
    shipping_address = models.ForeignKey('Address', on_delete=models.SET_NULL, null=True, blank=True)
    number = models.CharField(max_length=32, unique=True, null=True, blank=True, default=None)  # ← avoid ""
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["invoice_number"],
                condition=Q(invoice_number__isnull=False) & ~Q(invoice_number=""),
                name="uniq_invoice_number_nonempty",
            ),
        ]

    def __str__(self):
        return f"Order #{self.pk}"

    # Human invoice number base (unique per order pk/month)
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
        from server.models import Order as O  # safe self-import
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
        if creating and not self.number:
            # Assign order number once we have a PK
            self.number = f"ORD-{self.created_at:%Y%m%d}-{self.pk:06d}"
            super().save(update_fields=["number"])

    @property
    def payment_method_label(self):
        return self.get_payment_method_display()
    
    @property
    def return_deadline(self):
        if not self.delivered_at:
            return None
        return self.delivered_at + timedelta(days=1)

    @property
    def is_returnable(self) -> bool:
        if self.status != "delivered" or not self.delivered_at:
            return False
        return timezone.now() <= (self.delivered_at + timedelta(days=1))

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



# ✅ NEW: Invoice model to store PDF path
class Invoice(models.Model):
    order = models.OneToOneField("Order", on_delete=models.CASCADE, related_name="invoice")
    number = models.CharField(max_length=32, unique=True)
    pdf = models.FileField(upload_to="invoices/")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.number

class Review(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reviews")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="reviews")  # <- ensure this
    rating = models.PositiveIntegerField()
    message = models.TextField()
    image = models.ImageField(upload_to="review_images/", blank=True, null=True)
    # NEW (admin can override author display)
    author_name = models.CharField(max_length=100, blank=True, default="")
    author_avatar = models.ImageField(upload_to="review_avatars/", blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} on {self.product} ({self.rating})"