from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.template.loader import render_to_string, TemplateDoesNotExist
from django.utils import timezone
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from django.db import transaction

from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.core.mail import EmailMultiAlternatives

from .models import (
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

    Includes:
    - language (so we know which locale to prefer in emails)
    - date_of_birth (for birthday coupon logic)
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
        return {
            "email": user.email,
            "username": user.username,
            "full_name": getattr(prof, "full_name", "") or "",
            "mobile": getattr(prof, "mobile", "") or "",
            "image": prof.image.url if getattr(prof, "image", None) else None,
            "verified": bool(getattr(prof, "verified", False)),
            "language": getattr(prof, "language", "") or "en",
            "date_of_birth": getattr(prof, "date_of_birth", None),
        }

    def update(self, instance, validated_data):
        # instance is User
        if "username" in validated_data:
            instance.username = validated_data["username"]
            instance.save(update_fields=["username"])

        prof = instance.profile
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

    def create(self, validated_data):
        user = self.context["request"].user
        make_default = bool(validated_data.pop("is_default", False))

        # If frontend sent split street/house_number instead of address_line1
        if not validated_data.get("address_line1"):
            hn = self.initial_data.get("house_number") or ""
            sn = self.initial_data.get("street_name") or ""
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

    def update(self, instance, validated_data):
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
            thumb = getattr(obj, "thumbnail", None)
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
# Basic legacy order (single-line style)
# -----------------

class OrderSerializer(serializers.ModelSerializer):
    product = ProductSerializer()

    class Meta:
        model = Order
        fields = ["id", "product", "quantity", "created_at"]


# -----------------
# Order detail / items
# -----------------

class OrderItemReadSerializer(serializers.ModelSerializer):
    product = ProductMiniSerializer()
    line_total = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = ["id", "product", "quantity", "unit_price", "line_total"]

    def get_line_total(self, obj):
        try:
            return (
                Decimal(obj.unit_price) * obj.quantity
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except Exception:
            return Decimal("0.00")


class ShippingSnapshotSerializer(serializers.Serializer):
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    line1 = serializers.CharField()
    line2 = serializers.CharField()
    postal_code = serializers.CharField()
    city = serializers.CharField()
    country = serializers.CharField()
    phone = serializers.CharField()
    street_name = serializers.CharField(required=False, allow_blank=True)
    house_number = serializers.CharField(required=False, allow_blank=True)


class OrderReadSerializer(serializers.ModelSerializer):
    class ItemOut(serializers.ModelSerializer):
        line_total = serializers.SerializerMethodField()
        class Meta:
            model = OrderItem
            fields = ("product_id","product_name","quantity","unit_price","line_total")
        def get_line_total(self, obj):
            return f"{obj.line_total():.2f}"

    items = ItemOut(source="items.all", many=True, read_only=True)
    class Meta:
        model = Order
        fields = (
            "id","number","status","payment_method","installments_months",
            "email","total_price","ship_first_name","ship_last_name",
            "ship_line1","ship_line2","ship_postal","ship_city","ship_country",
            "ship_phone","created_at","items"
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
        request = self.context.get("request")
        # Prefer related Invoice model (Invoice.pdf)
        try:
            inv = getattr(obj, "invoice", None)
            f = getattr(inv, "pdf", None)
            if f and getattr(f, "url", None):
                return self._abs(request, f.url)
        except Exception:
            pass
        # Fallback: legacy FileField on Order
        try:
            f = getattr(obj, "invoice_pdf", None)
            if f and getattr(f, "url", None):
                return self._abs(request, f.url)
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
        return getattr(obj.coupon, "code", None)

    def get_payment_status(self, obj):
        # explicit payment_status if set, else infer from status
        return getattr(obj, "payment_status", "") or (
            "paid" if obj.status == "paid" else obj.status
        )


class ReturnRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReturnRequest
        fields = ("id", "request_type", "message", "status", "created_at")


# -----------------
# Checkout / create order
# -----------------



def quant2(val: Decimal) -> Decimal:
    return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

class ShippingInSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=120)
    last_name = serializers.CharField(max_length=120, allow_blank=True)
    line1 = serializers.CharField(max_length=255)
    line2 = serializers.CharField(max_length=255, allow_blank=True, required=False)
    postal_code = serializers.CharField(max_length=50)
    city = serializers.CharField(max_length=120)
    country = serializers.CharField(max_length=2)
    phone = serializers.CharField(max_length=64, allow_blank=True, required=False)

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
    installments_months = serializers.IntegerField(
        required=False, allow_null=True
    )
    payment_method_id = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )

    # optional for idempotency (frontend cart signature)
    idempotency_key = serializers.CharField(
        required=False, allow_blank=True
    )
        
    def _normalize_country(self, c: str) -> str:
        if not c:
            return "DE"
        c = c.strip()
        if len(c) == 2:
            return c.upper()
        mapping = {
            "Germany": "DE",
            "Deutschland": "DE",
            "Austria": "AT",
            "Österreich": "AT",
            "Switzerland": "CH",
            "Schweiz": "CH",
            "Italy": "IT",
            "Italia": "IT",
            "France": "FR",
            "Frankreich": "FR",
            "Netherlands": "NL",
            "Niederlande": "NL",
        }
        return mapping.get(c, "DE")

    def validate(self, attrs):
        req = self.context.get("request")
        user = getattr(req, "user", None)

        # Decide checkout mode
        is_auth = bool(
            user and user.is_authenticated and getattr(user, "is_active", True)
        )

        # Email is always required; for auth user we can fill it
        incoming_email = (attrs.get("email") or "").strip().lower()
        if is_auth and not incoming_email:
            incoming_email = (getattr(user, "email", "") or "").strip().lower()
        if not incoming_email:
            raise serializers.ValidationError({"email": "Email is required."})
        attrs["email"] = incoming_email

        # Normalize payment method from aliases
        aliases = {
            "stripe_sepa": "sepa_debit",
            "stripe_card": "card",
            "debit_card": "card",
            "card_installment": "card_installments",
            "card_installments": "card_installments",
            "sepa_installments": "sepa_debit_installments",
            "sepa": "sepa_debit",
            "manual_dd": "bank_transfer",
            "direct_debit": "bank_transfer",
            "bank": "bank_transfer",
            "bank_transfer": "bank_transfer",
            "paypal": "paypal",
            "giropay": "giropay",
            "klarna": "klarna",
        }
        key = (attrs.get("payment_method") or "").strip().lower() or "card"
        mapped = aliases.get(key, key)
        attrs["payment_method"] = mapped

        # Normalize + restrict country
        attrs["country"] = self._normalize_country(attrs.get("country") or "")
        if attrs["payment_method"] in (
            "sepa_debit",
            "sepa_debit_installments",
            "bank_transfer",
        ):
            if attrs["country"] != "DE":
                raise serializers.ValidationError(
                    {
                        "payment_method": "SEPA / Bank Transfer is only available in DE."
                    }
                )

        # Installments allowed only for certain methods
        months = attrs.get("months")
        if attrs["payment_method"] == "card_installments":
            if months is None or months not in (3, 6, 9):
                raise serializers.ValidationError(
                    {
                        "months": "Choose 3, 6, or 9 months for card installments."
                    }
                )
        else:
            attrs["months"] = None

        return attrs

   

    def _calc_prices(self, items_in):
        """
        Turn posted cart line items into:
        - clean_items: [(product, qty, unit_price_gross_decimal), ...]
        - subtotal (gross)
        """
        products = {}
        subtotal = Decimal("0.00")
        clean_items = []

        for it in items_in:
            pid = int(it["product_id"])
            qty = int(it["qty"])

            if qty <= 0:
                raise serializers.ValidationError(
                    {"items": "Quantity must be >= 1"}
                )

            if pid not in products:
                try:
                    products[pid] = Product.objects.get(pk=pid)
                except Product.DoesNotExist:
                    raise serializers.ValidationError(
                        {"items": f"Product {pid} not found"}
                    )

            p = products[pid]
            unit = Decimal(p.final_price_gross)  # gross snapshot

            clean_items.append((p, qty, unit))
            subtotal += (unit * qty)

        return clean_items, subtotal

    def _resolve_address_for_user(self, user, validated):
        """
        For authenticated users only:
        - try address_id
        - else dedupe match
        - else create new Address
        Guests skip this completely.
        """
        if not (
            user
            and user.is_authenticated
            and getattr(user, "is_active", True)
        ):
            return None

        aid = validated.get("address_id")
        if aid:
            a = Address.objects.filter(user=user, id=aid).first()
            if a:
                return a

        # dedupe signature from posted fields
        sig_fields = {
            "first_name": validated.get("first_name", "").strip(),
            "last_name": validated.get("last_name", "").strip(),
            "address_line1": validated.get("address_line1")
            or (
                f"{validated.get('street_name','')} "
                f"{validated.get('house_number','')}"
            ).strip(),
            "address_line2": validated.get("address_line2", ""),
            "postal_code": validated.get("postal_code", ""),
            "city": validated.get("city", ""),
            "country": validated.get("country", ""),
            "phone": validated.get("phone", ""),
        }

        for a in Address.objects.filter(user=user):
            if (
                a.first_name == sig_fields["first_name"]
                and a.last_name == sig_fields["last_name"]
                and a.address_line1 == sig_fields["address_line1"]
                and a.apartment == sig_fields["address_line2"]
                and a.postal_code == sig_fields["postal_code"]
                and a.city == sig_fields["city"]
                and a.country == sig_fields["country"]
                and a.phone == sig_fields["phone"]
            ):
                return a

        # Otherwise create new Address for this user
        line1 = sig_fields["address_line1"]
        addr = Address.objects.create(
            user=user,
            first_name=sig_fields["first_name"],
            last_name=sig_fields["last_name"],
            address_line1=line1,
            apartment=sig_fields["address_line2"],
            postal_code=sig_fields["postal_code"],
            city=sig_fields["city"],
            country=sig_fields["country"],
            phone=sig_fields["phone"],
            street_name=validated.get("street_name", ""),
            house_number=validated.get("house_number", ""),
        )

        # First address becomes default automatically
        if not Address.objects.filter(user=user).exclude(pk=addr.pk).exists():
            addr.set_default_for_user()

        return addr

    def _calc_subtotal(self, items_validated):
        total = Decimal("0.00")
        for it in items_validated:
            prod = Product.objects.get(pk=it["product_id"])
            unit = Decimal(str(getattr(prod, "final_price_gross", None) or prod.price_gross))
            total += unit * it["qty"]
        return quant2(total)

    def _apply_coupon(self, subtotal, code, user):
        if not code:
            return subtotal, Decimal("0.00")
        # hook your Coupon model logic. We'll fake -10% for demo.
        discount = (subtotal * Decimal("0.10")).quantize(Decimal("0.01"))
        total = subtotal - discount
        if total < Decimal("0.00"):
            total = Decimal("0.00")
        return total, discount

    def create(self, validated_data):
        request = self.context.get("request")
        user = request.user if request and request.user.is_authenticated else None

        items_data = validated_data["items"]
        shipping_data = validated_data["shipping"]
        coupon_code = validated_data.get("coupon_code", "").strip()
        payment_method = validated_data.get("payment_method")
        installments_months = validated_data.get("installments_months")
        idempotency_key = validated_data.get("idempotency_key", "").strip() or None

        # 1. compute totals
        subtotal = self._calc_subtotal(items_data)
        total_price, discount = self._apply_coupon(subtotal, coupon_code, user)

        # safety: Stripe/PayPal cannot charge 0 €
        if total_price <= Decimal("0.00"):
            raise serializers.ValidationError({
                "detail": "Cart total is 0.00 €. Cannot create online payment."
            })

        with transaction.atomic():
            # OPTIONAL: if you want idempotency: try to find existing 'pending' order with same idem key
            # Skipping here for clarity — you can implement if you store idempotency_key in Order.

            order = Order.objects.create(
                user=user if user else None,
                email=validated_data["email"].strip().lower(),
                total_price=total_price,
                payment_method=payment_method,
                installments_months=installments_months if installments_months else None,
                status="pending",
                ship_first_name=shipping_data["first_name"],
                ship_last_name=shipping_data["last_name"],
                ship_line1=shipping_data["line1"],
                ship_line2=shipping_data.get("line2", ""),
                ship_postal=shipping_data["postal_code"],
                ship_city=shipping_data["city"],
                ship_country=shipping_data["country"],
                ship_phone=shipping_data.get("phone", ""),
            )

            for it in items_data:
                prod = Product.objects.get(pk=it["product_id"])
                unit = Decimal(str(getattr(prod, "final_price_gross", None) or prod.price_gross))
                OrderItem.objects.create(
                    order=order,
                    product=prod,
                    product_name=prod.title_en or prod.title_de or str(prod),
                    quantity=it["qty"],
                    unit_price=unit,
                )

        return order
    


# -----------------
# Order list/detail for simple/legacy views
# -----------------

class OrderListItemSerializer(serializers.ModelSerializer):
    product = ProductMiniSerializer()

    class Meta:
        model = Order
        fields = ["id", "quantity", "total_price", "product"]


class OrderDetailSerializer(serializers.ModelSerializer):
    product = ProductMiniSerializer()
    shipping_address = AddressSerializer()

    class Meta:
        model = Order
        fields = [
            "id",
            "created_at",
            "status",
            "quantity",
            "total_price",
            "product",
            "shipping_address",
        ]


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
    image = serializers.SerializerMethodField()
    remaining_seconds = serializers.SerializerMethodField()

    class Meta:
        model = PromotionBanner
        fields = [
            "id",
            "headline",
            "subheadline",
            "code",
            "start_at",
            "end_at",
            "remaining_seconds",
            "bg_color",
            "text_color",
            "link_url",
            "image",
        ]

    def _abs(self, request, url: str) -> str:
        if not url:
            return ""
        if request and not str(url).startswith(("http://", "https://")):
            return request.build_absolute_uri(url)
        return url

    def get_code(self, obj):
        return obj.current_code()

    def get_image(self, obj):
        req = self.context.get("request")
        try:
            if obj.image and hasattr(obj.image, "url"):
                return self._abs(req, obj.image.url)
        except Exception:
            pass
        return ""

    def get_remaining_seconds(self, obj):
        n = timezone.now()
        if obj.end_at <= n:
            return 0
        delta = obj.end_at - n
        return max(0, int(delta.total_seconds()))


# -----------------
# Reviews
# -----------------

class ReviewSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(
        source="product.title_en", read_only=True
    )
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
        # prefer profile.full_name, fallback username, fallback email localpart
        if hasattr(u, "profile") and getattr(u.profile, "full_name", ""):
            return u.profile.full_name
        return u.username or (u.email.split("@")[0] if u.email else "User")

    def get_is_owner(self, obj):
        req = self.context.get("request")
        return bool(
            req and req.user.is_authenticated and obj.user_id == req.user.id
        )


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
            raise serializers.ValidationError(
                "Rating must be between 1 and 5."
            )
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
            raise serializers.ValidationError(
                {"quantity": "Must be at least 1."}
            )
        product = attrs.get("product")
        if not isinstance(product, Product):
            raise serializers.ValidationError({"product": "Invalid product."})
        return attrs


class StockAlertCreateSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(
        queryset=Product.objects.all()
    )
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
