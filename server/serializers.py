from decimal import Decimal, ROUND_HALF_UP
from sqlite3 import IntegrityError
from typing import Any, Dict, List, Mapping, Optional, Tuple, cast

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.template.loader import render_to_string, TemplateDoesNotExist
from django.utils import timezone
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from django.db import transaction, IntegrityError

from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.core.mail import EmailMultiAlternatives

from .models import (
    Cart,
    CartItem,
    CouponRedemption,
    PromotionBanner,
    StockAlert,
    ReturnRequest,
    NaturalStoneImage,
    OrderItem,
    User,
    Profile,
    Wishlist,
    NaturalStone,
    StoneEnquiry,
    Product,
    ProductMedia,
    ContactMessage,
    NewsletterSubscriber,
    Address,
    Order,
    Review,
    Coupon,
    OnDemandOrderRequest,
)

# -----------------
# Auth / Account
# -----------------

class AuthSerializer(serializers.Serializer):
    code = serializers.CharField(required=False)
    error = serializers.CharField(required=False)


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "email", "username")


class ProfileSerializer(serializers.ModelSerializer):
    """
    Profile surface for 'My Account' settings.
    """
    class Meta:
        model = Profile
        fields = [
            "full_name",
            "mobile",
            "image",
            "verified",
            "language",
            "date_of_birth",
        ]
        read_only_fields = ["verified"]


class AccountSerializer(serializers.Serializer):
    """
    Unified serializer for account overview/edit:
    - lets user change username (User model)
    - lets user change profile fields (Profile model)
    """
    # User-level
    email = serializers.EmailField(read_only=True)
    username = serializers.CharField(required=False, allow_blank=True)

    # Profile-level
    full_name = serializers.CharField(required=False, allow_blank=True)
    mobile = serializers.CharField(required=False, allow_blank=True)
    image = serializers.ImageField(required=False, allow_null=True)
    verified = serializers.BooleanField(read_only=True)
    language = serializers.CharField(required=False, allow_blank=True)
    date_of_birth = serializers.DateField(required=False, allow_null=True)

    def to_representation(self, instance):
        user = instance
        prof = getattr(user, "profile", None)

        image_url = None
        if prof and getattr(prof, "image", None):
            # guard both prof and image
            image_url = prof.image.url

        return {
            "email": user.email,
            "username": user.username,
            "full_name": getattr(prof, "full_name", "") or "",
            "mobile": getattr(prof, "mobile", "") or "",
            "image": image_url,
            "verified": bool(getattr(prof, "verified", False)),
            "language": getattr(prof, "language", "") or "en",
            "date_of_birth": getattr(prof, "date_of_birth", None),
        }

    def update(self, instance, validated_data):
        # instance is User
        if "username" in validated_data:
            instance.username = validated_data["username"]
            instance.save(update_fields=["username"])

        prof, _ = Profile.objects.get_or_create(user=instance)
        for f in ["full_name", "mobile", "image", "language", "date_of_birth"]:
            if f in validated_data:
                setattr(prof, f, validated_data[f])
        prof.save()
        return instance


# -----------------
# Addresses
# -----------------

class AddressSerializer(serializers.ModelSerializer):
    # Frontend expects "address_line2" but model calls it "apartment".
    address_line2 = serializers.CharField(
        source="apartment", allow_blank=True, required=False
    )

    class Meta:
        model = Address
        fields = [
            "id",
            "first_name",
            "last_name",
            "address_line1",
            "address_line2",
            "postal_code",
            "city",
            "country",
            "phone",
            "is_default",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def _sync_full_name(self, addr: Address):
        """
        Keep Address.full_name (legacy field) in sync.
        """
        full = " ".join(filter(None, [addr.first_name, addr.last_name])).strip()
        if full and addr.full_name != full:
            addr.full_name = full
            addr.save(update_fields=["full_name"])

    def create(self, validated_data: Dict[str, Any]) -> Address:
        # request can be missing in some contexts; guard it
        req = self.context.get("request")
        user = getattr(req, "user", None)
        make_default = bool(validated_data.pop("is_default", False))

        # Safely read raw incoming fields from initial_data (may be absent)
        init_data: Mapping[str, Any] = cast(
            Mapping[str, Any],
            getattr(self, "initial_data", {}) or {}
        )

        # If frontend sent split street/house_number instead of address_line1
        if not (validated_data.get("address_line1") or "").strip():
            hn = str(init_data.get("house_number") or "").strip()
            sn = str(init_data.get("street_name") or "").strip()
            synth = " ".join([sn, hn]).strip()
            if synth:
                validated_data["address_line1"] = synth

        # Always create first as non-default — then flip if needed.
        validated_data["is_default"] = False
        addr = Address.objects.create(user=user, **validated_data)

        is_first_for_user = not Address.objects.filter(user=user).exclude(pk=addr.pk).exists()
        if is_first_for_user or make_default:
            addr.set_default_for_user()

        self._sync_full_name(addr)
        return addr

    def update(self, instance, validated_data: Dict[str, Any]) -> Address:
        make_default = bool(validated_data.pop("is_default", False))

        for k, v in validated_data.items():
            setattr(instance, k, v)

        instance.save()

        if make_default:
            instance.set_default_for_user()

        self._sync_full_name(instance)
        return instance


# -----------------
# Wishlist
# -----------------

class WishlistSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product", read_only=True)

    class Meta:
        model = Wishlist
        fields = ["id", "product", "product_name"]


# -----------------
# JWT
# -----------------

class MyTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["full_name"] = getattr(user.profile, "full_name", "")
        token["username"] = user.username or ""
        token["email"] = user.email
        token["verified"] = getattr(user.profile, "verified", False)
        return token


# -----------------
# Registration
# -----------------

class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(
        write_only=True, required=True, validators=[validate_password]
    )
    password2 = serializers.CharField(write_only=True, required=True)

    class Meta:
        model = User
        fields = ("email", "username", "password", "password2")

    def validate(self, attrs):
        if attrs["password"] != attrs["password2"]:
            raise serializers.ValidationError(
                {"password": "Password fields didn't match."}
            )
        return attrs

    def create(self, validated_data):
        user = User(
            username=validated_data.get("username"),
            email=validated_data["email"],
            is_active=False,  # must verify email first
        )
        user.set_password(validated_data["password"])
        user.save()

        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        base = getattr(settings, "FRONTEND_URL", "https://mindmines.eu").rstrip("/")
        verification_link = f"{base}/verify-email/{uid}/{token}/"

        subject = "Verify your MindMines account"
        text_body = (
            "Please click the link to verify your email:\n" + verification_link
        )
        from_email = (
            getattr(settings, "DEFAULT_FROM_EMAIL", None)
            or getattr(settings, "EMAIL_HOST_USER", None)
        )

        html_body = None
        try:
            html_body = render_to_string(
                "emails/verification_email.html",
                {"user": user, "verification_link": verification_link},
            )
        except TemplateDoesNotExist:
            pass

        email = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=from_email,
            to=[user.email],
        )
        if html_body:
            email.attach_alternative(html_body, "text/html")
        email.send(fail_silently=not settings.DEBUG)

        return user


# -----------------
# Natural Stones
# -----------------

class NaturalStoneImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = NaturalStoneImage
        fields = ["id", "image"]


class NaturalStoneSerializer(serializers.ModelSerializer):
    images = NaturalStoneImageSerializer(many=True, read_only=True)

    class Meta:
        model = NaturalStone
        fields = [
            "id",
            "type",
            "description_en",
            "description_de",
            "interest_en",
            "interest_de",
            "key_features_en",
            "key_features_de",
            "usage_en",
            "usage_de",
            "image",
            "images",
        ]


class StoneEnquirySerializer(serializers.ModelSerializer):
    class Meta:
        model = StoneEnquiry
        fields = ["id", "stone_type", "name", "email", "message", "created_at"]


# -----------------
# Catalog
# -----------------

class ProductMediaSerializer(serializers.ModelSerializer):
    image = serializers.SerializerMethodField()
    video = serializers.SerializerMethodField()

    class Meta:
        model = ProductMedia
        fields = ["image", "video", "caption", "order", "media_type"]

    def _abs(self, request, url: str) -> str:
        if not url:
            return ""
        if request and not str(url).startswith(("http://", "https://")):
            return request.build_absolute_uri(url)
        return url

    def get_image(self, obj):
        request = self.context.get("request")
        try:
            if obj.image and hasattr(obj.image, "url"):
                return self._abs(request, obj.image.url)
        except Exception:
            pass
        return ""

    def get_video(self, obj):
        request = self.context.get("request")
        try:
            if obj.video and hasattr(obj.video, "url"):
                return self._abs(request, obj.video.url)
        except Exception:
            pass
        return ""


class ProductSerializer(serializers.ModelSerializer):
    media = ProductMediaSerializer(many=True, read_only=True)
    image = serializers.SerializerMethodField()
    stone_color_image_url = serializers.SerializerMethodField()

    # computed flags from model
    is_discounted = serializers.BooleanField(read_only=True)
    final_price_gross = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True
    )
    low_stock = serializers.BooleanField(read_only=True)
    out_of_stock = serializers.BooleanField(read_only=True)

    stock_status = serializers.SerializerMethodField()
    stock = serializers.SerializerMethodField()
    reviews_count = serializers.SerializerMethodField()
    average_rating = serializers.SerializerMethodField()
    price_net = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, allow_null=True
    )
    vat_rate = serializers.DecimalField(max_digits=4, decimal_places=4)

    class Meta:
        model = Product
        fields = [
            "id",
            "title_en",
            "title_de",
            "short_description1_en",
            "short_description1_de",
            "short_description2_en",
            "short_description2_de",
            "short_description3_en",
            "short_description3_de",
            "descriptionpara1_en",
            "descriptionpara1_de",
            "descriptionpara2_en",
            "descriptionpara2_de",
            "descriptionpara3_en",
            "descriptionpara3_de",
            "carepara_en",
            "carepara_de",
            "delivery_en",
            "delivery_de",
            "returnspara1_en",
            "returnspara1_de",
            "returnspara2_en",
            "returnspara2_de",
            "returnspara3_en",
            "returnspara3_de",
            "type_of_stone",
            "material",
            "color",
            "stone_color_image_url",
            "quantity",
            "dimensions",
            "weight",
            "category",
            # pricing
            "price_net",
            "vat_rate",
            "price_gross",
            "sale_price_gross",
            "discount_percent",
            "final_price_gross",
            "is_discounted",
            # misc
            "key",
            "image",
            "stock",
            "low_stock",
            "out_of_stock",
            "media",
            "stock_status",
            "reviews_count",
            "average_rating",
        ]

    def _abs(self, request, url: str) -> str:
        if not url:
            return ""
        if request and not str(url).startswith(("http://", "https://")):
            return request.build_absolute_uri(url)
        return url

    def get_image(self, obj):
        request = self.context.get("request")
        try:
            if obj.image and hasattr(obj.image, "url"):
                return self._abs(request, obj.image.url)
        except Exception:
            pass
        return ""

    def get_stone_color_image_url(self, obj):
        request = self.context.get("request")
        try:
            if obj.stone_color_image and hasattr(obj.stone_color_image, "url"):
                return self._abs(request, obj.stone_color_image.url)
        except Exception:
            pass
        return ""

    def get_stock(self, obj):
        return int(obj.quantity or 0)

    def get_stock_status(self, obj):
        return obj.stock_message()

    def get_reviews_count(self, obj):
        return getattr(obj, "reviews_count", 0) or 0

    def get_average_rating(self, obj):
        avg = getattr(obj, "average_rating", None)
        return round(float(avg), 1) if avg is not None else None


class ProductMiniSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField()
    title_en = serializers.CharField()
    title_de = serializers.CharField()
    image = serializers.SerializerMethodField()
    thumbnail_url = serializers.SerializerMethodField()
    price_gross = serializers.DecimalField(max_digits=10, decimal_places=2)
    final_price_gross = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id",
            "title_en",
            "title_de",
            "image",
            "thumbnail_url",
            "price_gross",
            "final_price_gross",
        ]

    def _abs(self, request, url: str) -> str:
        if not url:
            return ""
        if request and not str(url).startswith(("http://", "https://")):
            return request.build_absolute_uri(url)
        return url

    def get_image(self, obj):
        request = self.context.get("request")
        try:
            if getattr(obj, "image", None) and getattr(obj.image, "url", None):
                return self._abs(request, obj.image.url)
        except Exception:
            pass
        return ""

    def get_thumbnail_url(self, obj):
        request = self.context.get("request")
        try:
            thumb = getattr(obj, "thumbnail", None)  # optional
            if thumb and getattr(thumb, "url", None):
                return self._abs(request, thumb.url)
            if getattr(obj, "image", None) and getattr(obj.image, "url", None):
                return self._abs(request, obj.image.url)
        except Exception:
            pass
        return ""

    def get_final_price_gross(self, obj):
        try:
            return obj.final_price_gross
        except Exception:
            return obj.price_gross


# -----------------
# Contact / Newsletter
# -----------------

class ContactMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactMessage
        fields = ["id", "name", "email", "message", "sent_at"]

    def validate(self, attrs):
        if len(attrs.get("message", "").strip()) < 10:
            raise serializers.ValidationError("Message is too short.")
        return attrs


class NewsletterSubscriberSerializer(serializers.ModelSerializer):
    email = serializers.EmailField()

    class Meta:
        model = NewsletterSubscriber
        fields = ["id", "email", "subscribed_at"]

    def validate_email(self, value: str) -> str:
        return value.strip().lower()

# -----------------
# Cart detail / items
# -----------------
class CartItemSerializer(serializers.ModelSerializer):
    product = serializers.IntegerField(write_only=True)
    product_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = CartItem
        fields = ("product","product_id","name","image","price","qty","color","weight","dimensions","category")

    def validate(self, attrs):
        pid = attrs.pop("product", None)
        if pid is None:
            raise serializers.ValidationError({"product": "This field is required."})
        try:
            prod = Product.objects.only("id").get(pk=pid)
        except Product.DoesNotExist:
            raise serializers.ValidationError({"product": f"Invalid product id: {pid}"})
        attrs["product"] = prod
        return attrs

class CartSerializer(serializers.ModelSerializer):
    items = CartItemSerializer(many=True)
    user_email = serializers.SerializerMethodField()

    class Meta:
        model = Cart
        fields = ("id","user_email","cart_token","currency","is_active","created_at","updated_at","items")

    def get_user_email(self, obj):
        return getattr(obj.user, "email", None)

    def create(self, validated_data):
        items_data = validated_data.pop("items", [])
        cart = Cart.objects.create(**validated_data)
        self._upsert_items(cart, items_data)
        return cart

    def update(self, instance, validated_data):
        items_data = validated_data.pop("items", [])
        for k,v in validated_data.items():
            setattr(instance, k, v)
        instance.save(update_fields=["currency","is_active","updated_at"])
        # replace items by product_id uniqueness
        instance.items.all().delete()
        self._upsert_items(instance, items_data)
        return instance

    def _upsert_items(self, cart, items):
        bulk = []
        for it in items:
            bulk.append(CartItem(cart=cart, **it))
        CartItem.objects.bulk_create(bulk)

# -----------------
# Order detail / items
# -----------------

def quant2(val: Decimal) -> Decimal:
    return Decimal(val).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class OrderItemReadSerializer(serializers.ModelSerializer):
    product = ProductMiniSerializer()
    line_total = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = ["id", "product", "quantity", "unit_price", "line_total"]

    def get_line_total(self, obj):
        try:
            return quant2(Decimal(obj.unit_price) * obj.quantity)
        except Exception:
            return Decimal("0.00")


class OrderReadSerializer(serializers.ModelSerializer):
    class ItemOut(serializers.ModelSerializer):
        product_name = serializers.SerializerMethodField()
        line_total = serializers.SerializerMethodField()
        product_image = serializers.SerializerMethodField()

        class Meta:
            model = OrderItem
            fields = ("product_id", "product_name", "quantity", "unit_price", "line_total", "product_image")

        def get_product_name(self, obj):
            return obj.product_name  # uses property to prefer snapshot

        def get_line_total(self, obj):
            return f"{obj.line_total():.2f}"

        def get_product_image(self, obj):
            """
            Return an absolute URL for the product image when available.
            Falls back to empty string when the product or image is missing.
            """
            request = getattr(self, "context", {}).get("request")
            url = ""
            try:
                product = getattr(obj, "product", None)
                image = getattr(product, "image", None) if product else None
                if image is None and hasattr(obj, "product_image_snapshot"):
                    image = getattr(obj, "product_image_snapshot", None)
                if image is None:
                    return ""
                url = getattr(image, "url", image)
            except Exception:
                return ""

            if not url:
                return ""

            if request and not str(url).startswith(("http://", "https://")):
                return request.build_absolute_uri(url)
            return url

    items = ItemOut(source="items.all", many=True, read_only=True)

    # helper fields
    shipping_snapshot = serializers.SerializerMethodField()
    payment_method_label = serializers.SerializerMethodField()
    invoice_pdf = serializers.SerializerMethodField()
    is_returnable = serializers.SerializerMethodField()
    return_deadline = serializers.SerializerMethodField()
    coupon_code = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = (
            "id", "number",
            "status", "payment_status",
            "payment_method", "payment_method_label",
            "installments_months",
            "email",
            # money breakdown
            "subtotal", "discount_total", "shipping_price", "vat_total", "total_price",
            # shipping snapshot fields
            "ship_first_name", "ship_last_name", "ship_line1", "ship_line2",
            "ship_postal", "ship_city", "ship_country", "ship_phone",
            # logistics
            "tracking_number", "tracking_carrier", "tracking_url",
            "created_at", "shipped_at", "delivered_at",
            # derived / nested
            "items", "shipping_snapshot", "invoice_pdf",
            "is_returnable", "return_deadline", "coupon_code",
        )

    def get_shipping_snapshot(self, obj):
        return {
            "first_name": obj.ship_first_name,
            "last_name": obj.ship_last_name,
            "line1": obj.ship_line1,
            "line2": obj.ship_line2,
            "postal_code": obj.ship_postal,
            "city": obj.ship_city,
            "country": obj.ship_country,
            "phone": obj.ship_phone,
            "street_name": getattr(obj, "ship_street_name", None),
            "house_number": getattr(obj, "ship_house_number", None),
        }

    def get_payment_method_label(self, obj):
        try:
            return obj.get_payment_method_display()
        except Exception:
            return (obj.payment_method or "").title()

    def _abs(self, request, url):
        if not url:
            return url
        if request and not str(url).startswith(("http://", "https://")):
            return request.build_absolute_uri(url)
        return url

    def get_invoice_pdf(self, obj):
        request = self.context.get("request") if hasattr(self, "context") else None

        def _abs(url):
            if not url:
                return None
            s = str(url)
            if request and not s.startswith(("http://", "https://")):
                return request.build_absolute_uri(s)
            return s

        # Prefer related Invoice model (Invoice.pdf)
        try:
            inv = getattr(obj, "invoice", None)
            if inv is not None:
                f = getattr(inv, "pdf", None)
                if f is not None:
                    url = getattr(f, "url", None)
                    if url:
                        return _abs(url)
        except Exception:
            # don't break the whole orders endpoint if invoice is weird
            pass
        
        # Fallback: legacy FileField on Order (if ever existed)
        try:
            f = getattr(obj, "invoice_pdf", None)
            if f is not None:
                url = getattr(f, "url", None)
                if url:
                    return _abs(url)
        except Exception:
            pass
        return None

    def get_is_returnable(self, obj):
        try:
            return obj.is_returnable
        except Exception:
            return False

    def get_return_deadline(self, obj):
        try:
            rd = obj.return_deadline
            return rd.isoformat() if rd else None
        except Exception:
            return None

    def get_coupon_code(self, obj):
        try:
            red = getattr(obj, "coupon_redemption", None)
            if red and red.coupon:
                return red.coupon.code
        except Exception:
            pass
        return None


class ReturnRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReturnRequest
        fields = ("id", "request_type", "message", "status", "created_at")


# -----------------
# Checkout / create order
# -----------------

class ShippingInSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=120)
    last_name  = serializers.CharField(max_length=120, allow_blank=True)
    line1      = serializers.CharField(max_length=255)
    line2      = serializers.CharField(max_length=255, allow_blank=True, required=False)
    postal_code= serializers.CharField(max_length=50)
    city       = serializers.CharField(max_length=120)
    country    = serializers.CharField(max_length=2)
    phone      = serializers.CharField(max_length=64, allow_blank=True, required=False)
    # Optional (shown in admin if available)
    street_name = serializers.CharField(max_length=255, allow_blank=True, required=False)
    house_number = serializers.CharField(max_length=64,  allow_blank=True, required=False)


class OrderItemIn(serializers.Serializer):
    product_id = serializers.IntegerField()
    qty = serializers.IntegerField(min_value=1, max_value=99)


class OrderCreateSerializer(serializers.Serializer):
    email = serializers.EmailField()
    items = OrderItemIn(many=True)
    shipping = ShippingInSerializer()
    coupon_code = serializers.CharField(max_length=64, allow_blank=True, required=False)

    # payment selection
    payment_method = serializers.CharField(max_length=64)

    # installments inputs
    installments_months = serializers.IntegerField(required=False, allow_null=True)
    payment_method_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    # optional for idempotency (frontend cart signature)
    idempotency_key = serializers.CharField(required=False, allow_blank=True)

    # --- helpers ---

    def _normalize_country(self, c: Optional[str]) -> str:
        if not c:
            return "DE"
        c = c.strip()
        if len(c) == 2:
            return c.upper()
        mapping: Dict[str, str] = {
            "Germany": "DE", "Deutschland": "DE",
            "Austria": "AT", "Österreich": "AT",
            "Switzerland": "CH", "Schweiz": "CH",
            "Italy": "IT", "Italia": "IT",
            "France": "FR", "Frankreich": "FR",
            "Netherlands": "NL", "Niederlande": "NL",
        }
        return mapping.get(c, "DE")

    # --- validation ---

    def validate(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
        req = self.context.get("request")
        user = getattr(req, "user", None)

        # auth mode?
        is_auth = bool(user and getattr(user, "is_authenticated", False) and getattr(user, "is_active", True))

        # email
        incoming_email = (attrs.get("email") or "").strip().lower()
        if is_auth and not incoming_email:
            incoming_email = (getattr(user, "email", "") or "").strip().lower()
        if not incoming_email:
            raise serializers.ValidationError({"email": "Email is required."})
        attrs["email"] = incoming_email

        # normalize payment method aliases
        aliases: Dict[str, str] = {
            "stripe_sepa": "sepa_debit",
            "stripe_card": "card",
            "debit_card": "card",
            "card_installment": "card_installments",
            "card_installments": "card_installments",
            "sepa_debit_installments": "sepa_installments",
            "sepa_installments": "sepa_installments",
            "sepa": "sepa_debit",
            "paypal": "paypal",
            "giropay": "giropay",
            "klarna": "klarna_single",        
            "klarna_single": "klarna_single",
            "klarna_installments": "klarna_installments",
        }
        key = str((attrs.get("payment_method") or "")).strip().lower() or "card"
        attrs["payment_method"] = aliases.get(key, key)

        # country is inside shipping (ensure it's a dict)
        shipping_in: Dict[str, Any] = cast(Dict[str, Any], attrs.get("shipping") or {})
        country_in = cast(Optional[str], shipping_in.get("country"))
        country_norm = self._normalize_country(country_in)
        shipping_in["country"] = country_norm
        attrs["shipping"] = shipping_in

        # methods restricted to DE (tune as needed)
        if attrs["payment_method"] in ("sepa_debit", "sepa_installments", "bank_transfer"):
            if country_norm != "DE":
                raise serializers.ValidationError({
                    "payment_method": "SEPA / Bank Transfer is only available in DE."
                })

        # installments validation — allow alias via initial_data["months"]
        init_data: Mapping[str, Any] = cast(Mapping[str, Any], getattr(self, "initial_data", {}) or {})
        if attrs.get("installments_months") in (None, ""):
            try:
                alias = int(init_data.get("months") or 0)
                if alias in (3, 6, 9):
                    attrs["installments_months"] = alias
            except Exception:
                pass

        months = cast(Optional[int], attrs.get("installments_months"))
        if attrs["payment_method"] == "card_installments":
            if months is None or months not in (3, 6, 9):
                raise serializers.ValidationError({"installments_months": "Choose 3, 6, or 9 months for card installments."})
        elif attrs["payment_method"] == "sepa_installments":
            if months is None or months not in (3, 6, 9):
                raise serializers.ValidationError({"installments_months": "Choose 3, 6, or 9 months for SEPA installments."})
        else:
            attrs["installments_months"] = None

        return attrs

    # --- pricing & coupon ---

    def _calc_subtotal(self, items_validated: List[Dict[str, Any]]) -> Decimal:
        total = Decimal("0.00")
        for it in items_validated:
            pid = int(it["product_id"])  # validated by OrderItemIn
            qty = int(it["qty"])
            prod = Product.objects.get(pk=pid)
            unit = Decimal(str(getattr(prod, "final_price_gross", None) or prod.price_gross))
            total += unit * qty
        return quant2(total)

    def _apply_coupon(self, subtotal: Decimal, code: str, user: Optional[Any]) -> Tuple[Decimal, Decimal, Optional["Coupon"]]:
        if not code:
            return subtotal, Decimal("0.00"), None

        coup = Coupon.objects.filter(code__iexact=code.strip()).first()
        if not coup or not coup.active:
            return subtotal, Decimal("0.00"), None

        # validated_data may be “empty” at type time; guard + cast to Mapping
        vd: Mapping[str, Any] = cast(Mapping[str, Any], getattr(self, "validated_data", {}) or {})
        raw_items = vd.get("items") or []  # safe even if missing/None

        # ensure a concrete list[dict] for the coupon API
        items_for_coupon: List[Dict[str, Any]] = cast(List[Dict[str, Any]], raw_items)

        ok, _ = coup.can_apply(user, items_for_coupon, subtotal)
        if not ok:
            return subtotal, Decimal("0.00"), None

        discount = quant2(coup.discount_amount(user, items_for_coupon, subtotal))
        total = quant2(subtotal - discount)
        if total < Decimal("0.00"):
            total = Decimal("0.00")
        return total, discount, coup

    # --- creation ---

    def create(self, validated_data: Dict[str, Any]) -> Order:
        req = self.context.get("request")
        user = getattr(req, "user", None)
        if not (user and getattr(user, "is_authenticated", False)):
            user = None

        items_data: List[Dict[str, Any]] = cast(List[Dict[str, Any]], validated_data.pop("items", []))
        shipping_data: Dict[str, Any] = cast(Dict[str, Any], validated_data.pop("shipping", {}) or {})
        coupon_code = str((validated_data.get("coupon_code") or "")).strip()
        payment_method = str(validated_data.get("payment_method") or "")
        installments_months = cast(Optional[int], validated_data.get("installments_months"))
        idempotency_key = str((validated_data.get("idempotency_key") or "")).strip() or None 

        # If a key is supplied and an order already exists, return it (idempotent)
        # if idempotency_key:
        #     existing = Order.objects.filter(idempotency_key=idempotency_key).first()
        #     if existing:
        #         return existing

        # totals (preliminary)
        subtotal = self._calc_subtotal(items_data)
        total_after_discount, discount, coupon_obj = self._apply_coupon(subtotal, coupon_code, user)

        # safety: online payment cannot be 0 €
        if total_after_discount <= Decimal("0.00"):
            raise serializers.ValidationError({
                "detail": "Your total is 0.00 € after discounts. We can't create an online payment for 0 €. Please remove the full discount or add another item."
            })


        with transaction.atomic():
           
            order = Order.objects.create(
                user=user if user else None,
                email=str(validated_data.get("email") or ""),

                # money fields (shipping/vat will be recomputed below)
                subtotal=subtotal,
                discount_total=discount,
                shipping_price=Decimal("0.00"),
                vat_total=Decimal("0.00"),
                total_price=total_after_discount,

                # payment
                payment_method=payment_method,
                installments_months=installments_months if installments_months else None,
                stock_locked=False,
                stock_released=False,
                status="pending",
                # snapshot shipping
                ship_first_name=str(shipping_data.get("first_name") or ""),
                ship_last_name=str(shipping_data.get("last_name") or ""),
                ship_line1=str(shipping_data.get("line1") or ""),
                ship_line2=str(shipping_data.get("line2") or ""),
                ship_postal=str(shipping_data.get("postal_code") or ""),
                ship_city=str(shipping_data.get("city") or ""),
                ship_country=str(shipping_data.get("country") or ""),
                ship_phone=str(shipping_data.get("phone") or ""),
                ship_street_name=str(shipping_data.get("street_name") or ""),
                ship_house_number=str(shipping_data.get("house_number") or ""),
            )
           

            # Create items and compute using product.final_price_gross
            for it in items_data:
                pid = int(it["product_id"])
                qty = int(it["qty"])
                prod = Product.objects.select_for_update().get(pk=pid)
                unit = Decimal(str(getattr(prod, "final_price_gross", None) or prod.price_gross))
                OrderItem.objects.create(
                    order=order,
                    product=prod,
                    product_name_snapshot=(prod.title_en or prod.title_de or str(prod)),
                    quantity=qty,
                    unit_price=unit,
                )

            # attach coupon to order NOW so UI can show it under the input
            if coupon_obj:
                CouponRedemption.objects.create(
                    coupon=coupon_obj,
                    user=user if user else None,
                    order=order,
                )

            # If you want to attach coupon to order via CouponRedemption, do it in your view/service:
            #   CouponRedemption.objects.create(coupon=coupon_obj, user=user if user else None, order=order)
            # Here we just recompute totals to populate vat_total, etc.
            order.compute_totals(shipping_price=order.shipping_price)  # uses default VAT
            order.save(update_fields=["subtotal", "discount_total", "shipping_price", "vat_total", "total_price"])

        return order


# -----------------
# Order list/detail (compact)
# -----------------

class OrderListItemSerializer(serializers.ModelSerializer):
    items_count = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ["id", "number", "status", "payment_status", "total_price", "created_at", "items_count"]

    def get_items_count(self, obj):
        try:
            return obj.items.count()
        except Exception:
            return 0


# -----------------
# Coupon Preview
# -----------------

class CouponPreviewIn(serializers.Serializer):
    code = serializers.CharField()
    items = OrderItemIn(many=True)


# -----------------
# Promotions
# -----------------

class PromotionSerializer(serializers.ModelSerializer):
    code = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = PromotionBanner
        fields = [
            "id", "name",
            "headline", "subheadline",
            "bg_color", "text_color",
            "link_url", "image_url",
            "start_at", "end_at", "is_enabled",
            "priority", "code",
        ]

    def get_code(self, obj):
        return obj.current_code()

    def get_image_url(self, obj):
        request = self.context.get("request")
        img = getattr(obj, "image", None)
        if not img:
            return None
        url = getattr(img, "url", None)
        if not url:
            return None
        if request and not str(url).startswith(("http://", "https://")):
            return request.build_absolute_uri(url)
        return url


# -----------------
# Reviews
# -----------------

class ReviewSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.title_en", read_only=True)
    image = serializers.SerializerMethodField()
    author_avatar_url = serializers.SerializerMethodField()
    user_name = serializers.SerializerMethodField()
    is_owner = serializers.SerializerMethodField()

    class Meta:
        model = Review
        fields = [
            "id",
            "product",
            "product_name",
            "rating",
            "message",
            "image",
            "author_avatar_url",
            "author_name",
            "created_at",
            "user_name",
            "is_owner",
        ]
        read_only_fields = [
            "product",
            "product_name",
            "user_name",
            "is_owner",
            "author_avatar_url",
        ]

    def _abs_url(self, request, f):
        if not f:
            return None
        try:
            url = f.url
        except Exception:
            return None
        return request.build_absolute_uri(url) if request else url

    def get_image(self, obj):
        return self._abs_url(self.context.get("request"), obj.image)

    def get_author_avatar_url(self, obj):
        return self._abs_url(self.context.get("request"), obj.author_avatar)

    def get_user_name(self, obj):
        if obj.author_name:
            return obj.author_name
        u = obj.user
        if hasattr(u, "profile") and getattr(u.profile, "full_name", ""):
            return u.profile.full_name
        return u.username or (u.email.split("@")[0] if u.email else "User")

    def get_is_owner(self, obj):
        req = self.context.get("request")
        return bool(req and req.user.is_authenticated and obj.user_id == req.user.id)


class ReviewWriteSerializer(serializers.ModelSerializer):
    user = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), required=False, write_only=True
    )

    class Meta:
        model = Review
        fields = [
            "rating",
            "message",
            "image",
            "author_name",
            "author_avatar",
            "user",
        ]

    def validate_rating(self, v):
        if v < 1 or v > 5:
            raise serializers.ValidationError("Rating must be between 1 and 5.")
        return v


# -----------------
# Stock Alert & On-Demand Requests
# -----------------

class OnDemandOrderRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = OnDemandOrderRequest
        fields = [
            "id",
            "product",
            "name",
            "email",
            "quantity",
            "message",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def validate(self, attrs):
        qty = int(attrs.get("quantity") or 0)
        if qty < 1:
            raise serializers.ValidationError({"quantity": "Must be at least 1."})
        product = attrs.get("product")
        if not isinstance(product, Product):
            raise serializers.ValidationError({"product": "Invalid product."})
        return attrs


class StockAlertCreateSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    email = serializers.EmailField()

    def validate_email(self, v):
        return (v or "").strip().lower()

    def create(self, validated_data):
        alert, created = StockAlert.objects.get_or_create(
            product=validated_data["product"],
            email=validated_data["email"],
            defaults={"is_active": True},
        )
        if not created and not alert.is_active:
            alert.is_active = True
            alert.notified_at = None
            alert.save(update_fields=["is_active", "notified_at"])
        return alert
