#server/serializers.py
from rest_framework import serializers
from django.utils import timezone
from django.utils.timezone import now
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from django.contrib.auth.password_validation import validate_password
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.conf import settings
from django.template.loader import render_to_string, TemplateDoesNotExist
from django.core.mail import EmailMultiAlternatives
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth import get_user_model
from django.db import transaction

from decimal import Decimal, ROUND_HALF_UP
from datetime import timedelta

from .models import (
    PromotionBanner, StockAlert, ReturnRequest, NaturalStoneImage, OrderItem, User, Profile,
    Wishlist, NaturalStone, StoneEnquiry, Product, ProductMedia, ContactMessage,
    NewsletterSubscriber, Address, Order, Review, Coupon, OnDemandOrderRequest
)

# ----------------- Helpers -----------------

def _norm(s):
    return " ".join((s or "").strip().split()).lower()

def _addr_signature_dict(validated):
    line1 = validated.get("address_line1") or " ".join([
        validated.get("street_name",""), validated.get("house_number","")
    ]).strip()
    return (
        _norm(validated.get("first_name")),
        _norm(validated.get("last_name")),
        _norm(line1),
        _norm(validated.get("address_line2")),
        _norm(validated.get("postal_code")),
        _norm(validated.get("city")),
        _norm(validated.get("country")),
        _norm(validated.get("phone")),
    )

def _addr_signature_model(addr):
    line1 = addr.address_line1 or f"{addr.street_name} {addr.house_number}".strip()
    return (
        _norm(addr.first_name),
        _norm(addr.last_name),
        _norm(line1),
        _norm(addr.apartment),
        _norm(addr.postal_code or addr.pincode),
        _norm(addr.city or addr.state),
        _norm(addr.country),
        _norm(addr.phone),
    )

# ----------------- Auth / Account -----------------

class AuthSerializer(serializers.Serializer):
    code = serializers.CharField(required=False)
    error = serializers.CharField(required=False)

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "email", "username")

class ProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = Profile
        fields = ['full_name', 'mobile', 'image', 'verified']
        read_only_fields = ['verified']

class AccountSerializer(serializers.Serializer):
    email = serializers.EmailField(read_only=True)
    username = serializers.CharField(required=False, allow_blank=True)
    full_name = serializers.CharField(required=False, allow_blank=True)
    mobile = serializers.CharField(required=False, allow_blank=True)
    image = serializers.ImageField(required=False, allow_null=True)
    verified = serializers.BooleanField(read_only=True)

    def to_representation(self, instance):
        user = instance
        prof = getattr(user, "profile", None)
        return {
            "email": user.email,
            "username": user.username,
            "full_name": getattr(prof, "full_name", ""),
            "mobile": getattr(prof, "mobile", ""),
            "image": prof.image.url if getattr(prof, "image", None) else None,
            "verified": getattr(prof, "verified", False),
        }

    def update(self, instance, validated_data):
        if "username" in validated_data:
            instance.username = validated_data["username"]
            instance.save(update_fields=["username"])

        prof = instance.profile
        for f in ["full_name", "mobile", "image"]:
            if f in validated_data:
                setattr(prof, f, validated_data[f])
        prof.save()
        return instance

# ----------------- Addresses -----------------

class AddressSerializer(serializers.ModelSerializer):
    address_line2 = serializers.CharField(source="apartment", allow_blank=True, required=False)

    class Meta:
        model = Address
        fields = [
            "id",
            "first_name", "last_name",
            "address_line1", "address_line2",
            "postal_code", "city", "country", "phone",
            "is_default",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def _sync_full_name(self, addr):
        full = " ".join(filter(None, [addr.first_name, addr.last_name])).strip()
        if full and addr.full_name != full:
            addr.full_name = full
            addr.save(update_fields=["full_name"])

    def create(self, validated_data):
        user = self.context["request"].user
        make_default = bool(validated_data.pop("is_default", False))

        if not validated_data.get("address_line1"):
            hn = self.initial_data.get("house_number") or ""
            sn = self.initial_data.get("street_name") or ""
            synth = " ".join([sn, hn]).strip()
            if synth:
                validated_data["address_line1"] = synth

        # ✅ Always insert as non-default, then flip
        validated_data["is_default"] = False
        addr = Address.objects.create(user=user, **validated_data)

        is_first = not Address.objects.filter(user=user).exclude(pk=addr.pk).exists()
        if is_first or make_default:
            addr.set_default_for_user()

        self._sync_full_name(addr)
        return addr

    def update(self, instance, validated_data):
        make_default = bool(validated_data.pop("is_default", False))
        for k, v in validated_data.items():
            setattr(instance, k, v)
        # ✅ Save without toggling default first
        instance.save()

        if make_default:
            instance.set_default_for_user()

        self._sync_full_name(instance)
        return instance
# ----------------- Wishlist -----------------

class WishlistSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product", read_only=True)
    class Meta:
        model = Wishlist
        fields = ["id", "product", "product_name"]


# ----------------- JWT -----------------

class MyTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["full_name"] = getattr(user.profile, "full_name", "")
        token["username"] = user.username or ""
        token["email"] = user.email
        token["verified"] = getattr(user.profile, "verified", False)
        return token


# ----------------- Registration -----------------

class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=True, validators=[validate_password])
    password2 = serializers.CharField(write_only=True, required=True)

    class Meta:
        model = User
        fields = ('email', 'username', 'password', 'password2')

    def validate(self, attrs):
        if attrs['password'] != attrs['password2']:
            raise serializers.ValidationError({"password": "Password fields didn't match."})
        return attrs

    def create(self, validated_data):
        user = User(
            username=validated_data.get("username"),
            email=validated_data["email"],
            is_active=False,   # requires verification
        )
        user.set_password(validated_data["password"])
        user.save()

        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        base = getattr(settings, "FRONTEND_URL", "https://mindmines.eu").rstrip("/")
        verification_link = f"{base}/verify-email/{uid}/{token}/"

        subject = "Verify your MindMines account"
        text_body = f"Please click the link to verify your email:\n{verification_link}"
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or getattr(settings, "EMAIL_HOST_USER", None)

        html_body = None
        try:
            html_body = render_to_string("emails/verification_email.html", {
                "user": user,
                "verification_link": verification_link,
            })
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


# ----------------- Natural Stones -----------------

class NaturalStoneImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = NaturalStoneImage
        fields = ["id", "image"]

class NaturalStoneSerializer(serializers.ModelSerializer):
    images = NaturalStoneImageSerializer(many=True, read_only=True)
    class Meta:
        model = NaturalStone
        fields = [
            "id", "type",
            "description_en", "description_de",
            "interest_en", "interest_de",
            "key_features_en", "key_features_de",
            "usage_en", "usage_de",
            "image", "images"
        ]


class StoneEnquirySerializer(serializers.ModelSerializer):
    class Meta:
        model = StoneEnquiry
        fields = ["id", "stone_type", "name", "email", "message", "created_at"]


# ----------------- Catalog -----------------

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

    # computed flags/values from model
    is_discounted = serializers.BooleanField(read_only=True)
    final_price_gross = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    low_stock = serializers.BooleanField(read_only=True)
    out_of_stock = serializers.BooleanField(read_only=True)

    stock_status = serializers.SerializerMethodField()
    stock = serializers.SerializerMethodField()
    reviews_count = serializers.SerializerMethodField()
    average_rating = serializers.SerializerMethodField()
    price_net = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True)
    vat_rate = serializers.DecimalField(max_digits=4, decimal_places=4)

    class Meta:
        model = Product
        fields = [
            "id", "title_en", "title_de",
            "short_description1_en", "short_description1_de",
            "short_description2_en", "short_description2_de",
            "short_description3_en", "short_description3_de",
            "descriptionpara1_en", "descriptionpara1_de",
            "descriptionpara2_en", "descriptionpara2_de",
            "descriptionpara3_en", "descriptionpara3_de",
            "carepara_en", "carepara_de",
            "delivery_en", "delivery_de",
            "returnspara1_en", "returnspara1_de",
            "returnspara2_en", "returnspara2_de",
            "returnspara3_en", "returnspara3_de",
            "type_of_stone", "material", "color",
            "stone_color_image_url",
            "quantity", "dimensions", "weight", "category",
            # pricing (gross)
            "price_net", "vat_rate",
            "price_gross", "sale_price_gross", "discount_percent",
            "final_price_gross", "is_discounted",
            # misc
            "key", "image", "stock", "low_stock", "out_of_stock",
            "media", "stock_status",
            "reviews_count", "average_rating",
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
    # expose gross pricing
    price_gross = serializers.DecimalField(max_digits=10, decimal_places=2)
    final_price_gross = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = ["id", "title_en", "title_de", "image", "thumbnail_url", "price_gross", "final_price_gross"]

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


# ----------------- Contact / Newsletter -----------------

class ContactMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactMessage
        fields = ["id", "name", "email", "message", "sent_at"]

    def validate(self, attrs):
        if len(attrs.get("message","").strip()) < 10:
            raise serializers.ValidationError("Message is too short.")
        return attrs

class NewsletterSubscriberSerializer(serializers.ModelSerializer):
    email = serializers.EmailField()

    class Meta:
        model = NewsletterSubscriber
        fields = ["id", "email", "subscribed_at"]

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


# ----------------- Basic Order (legacy single-line) -----------------

class OrderSerializer(serializers.ModelSerializer):
    product = ProductSerializer()
    class Meta:
        model = Order
        fields = ["id", "product", "quantity", "created_at"]


# ----------------- Orders (items) -----------------

class OrderItemReadSerializer(serializers.ModelSerializer):
    product = ProductMiniSerializer()
    line_total = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = ["id", "product", "quantity", "unit_price", "line_total"]

    def get_line_total(self, obj):
        try:
            return (Decimal(obj.unit_price) * obj.quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
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

class OrderReadSerializer(serializers.ModelSerializer):
    items = OrderItemReadSerializer(many=True, read_only=True)
    invoice_pdf = serializers.SerializerMethodField()
    shipping_address = AddressSerializer(read_only=True)
    shipping_snapshot = serializers.SerializerMethodField()
    is_returnable = serializers.SerializerMethodField()
    return_deadline = serializers.SerializerMethodField()
    payment_method_label = serializers.SerializerMethodField()
    coupon_code = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            "id", "number", "status", "payment_method", "payment_method_label",
            "subtotal", "discount_amount", "shipping_fee", "total_price",
            "created_at", "updated_at", "tracking_number",
            "items", "invoice_pdf", "is_returnable", "return_deadline",
            "shipping_address", "shipping_snapshot", "coupon_code",
            "email",
        ]

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
        # 1) Invoice relation (preferred)
        try:
            inv = getattr(obj, "invoice", None)
            f = getattr(inv, "pdf", None)
            if f and getattr(f, "url", None):
                return self._abs(request, f.url)
        except Exception:
            pass
        # 2) Legacy on Order
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


class ReturnRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReturnRequest
        fields = ("id", "request_type", "message", "status", "created_at")


# ----------------- Order Create (cart → order pending) -----------------

class OrderItemIn(serializers.Serializer):
    product_id = serializers.IntegerField()
    qty = serializers.IntegerField(min_value=1)

class OrderCreateSerializer(serializers.Serializer):
    email = serializers.EmailField(required=False, allow_blank=True)
    items = OrderItemIn(many=True)

    payment_method = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
    )

    coupon_code = serializers.CharField(allow_blank=True, required=False)

    # shipping address input (always collected)
    address_id = serializers.IntegerField(required=False)
    first_name = serializers.CharField(allow_blank=True, required=False)
    last_name  = serializers.CharField(allow_blank=True, required=False)
    address_line1 = serializers.CharField(allow_blank=True, required=False)
    address_line2 = serializers.CharField(allow_blank=True, required=False)
    postal_code = serializers.CharField(allow_blank=True, required=False)
    city = serializers.CharField(allow_blank=True, required=False)
    country = serializers.CharField(allow_blank=True, required=False)
    phone = serializers.CharField(allow_blank=True, required=False)
    is_default = serializers.BooleanField(required=False)
    # DE-style split fields from frontend (optional)
    house_number = serializers.CharField(allow_blank=True, required=False)
    street_name  = serializers.CharField(allow_blank=True, required=False)

    # optional installments hint
    method = serializers.ChoiceField(
        choices=[
            "card", "card_installments",
            "sepa_debit", "sepa_debit_installments",
            "paypal", "bank_transfer",
            "giropay", "klarna",
        ],
        required=False
    )
    months = serializers.IntegerField(required=False, min_value=2, max_value=24)
    idempotency_key = serializers.CharField(required=False, allow_blank=True)

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
        is_auth = bool(user and user.is_authenticated and getattr(user, "is_active", True))

        # email requirement
        incoming_email = (attrs.get("email") or "").strip().lower()
        if is_auth:
            if not incoming_email:
                incoming_email = (getattr(user, "email", "") or "").strip().lower()
        if not incoming_email:
            raise serializers.ValidationError({"email": "Email is required."})
        attrs["email"] = incoming_email

        # normalize payment method / aliases
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
            "giropay": "giropay",   # frontend will use Stripe PaymentElement (redirect), we'll treat as card intent
            "klarna": "klarna",
        }
        key = (attrs.get("payment_method") or "").strip().lower() or "card"
        mapped = aliases.get(key, key)
        attrs["payment_method"] = mapped

        # country normalization
        attrs["country"] = self._normalize_country(attrs.get("country") or "")

        # rules: SEPA + manual bank -> DE only
        if attrs["payment_method"] in ("sepa_debit", "sepa_debit_installments", "bank_transfer"):
            if attrs["country"] != "DE":
                raise serializers.ValidationError(
                    {"payment_method": "SEPA / Bank Transfer is only available in DE."}
                )

        # installments rule for card_installments (we also reuse same choices)
        months = attrs.get("months")
        if attrs["payment_method"] == "card_installments":
            if months is None or months not in (3, 6, 9):
                raise serializers.ValidationError(
                    {"months": "Choose 3, 6, or 9 months for card installments."}
                )
        else:
            attrs["months"] = None  # ignore months if not card_installments

        return attrs

    def _apply_coupon(self, user, items, subtotal, code):
        if not code:
            return None, Decimal("0.00"), None
        coup = Coupon.objects.filter(code__iexact=str(code).strip()).first()
        if not coup:
            raise serializers.ValidationError({"coupon_code": "Coupon not found."})
        ok, reason = coup.can_apply(user if getattr(user, "is_authenticated", False) else None, items, subtotal)
        if not ok:
            raise serializers.ValidationError({"coupon_code": reason})
        disc = coup.discount_amount(user if getattr(user, "is_authenticated", False) else None, items, subtotal)
        return coup, disc, None

    def _calc_prices(self, items_in):
        """Resolve products, build clean line list, calc subtotal."""
        products = {}
        subtotal = Decimal("0.00")
        clean_items = []
        for it in items_in:
            pid = int(it["product_id"])
            qty = int(it["qty"])
            if qty <= 0:
                raise serializers.ValidationError({"items": "Quantity must be >= 1"})
            if pid not in products:
                try:
                    products[pid] = Product.objects.get(pk=pid)
                except Product.DoesNotExist:
                    raise serializers.ValidationError({"items": f"Product {pid} not found"})
            p = products[pid]
            unit = Decimal(p.final_price_gross)
            clean_items.append((p, qty, unit))
            subtotal += (unit * qty)
        return clean_items, subtotal

    def _resolve_address_for_user(self, user, validated):
        """
        If user is authenticated:
        - try address_id
        - try dedupe existing
        - else create Address(user=...)
        Guests do NOT go here.
        """
        if not (user and user.is_authenticated and getattr(user, "is_active", True)):
            return None

        aid = validated.get("address_id")
        if aid:
            a = Address.objects.filter(user=user, id=aid).first()
            if a:
                return a

        # build sig for dedupe
        sig_fields = {
            "first_name": validated.get("first_name", "").strip(),
            "last_name":  validated.get("last_name", "").strip(),
            "address_line1": validated.get("address_line1") or (
                f"{validated.get('street_name','')} {validated.get('house_number','')}".strip()
            ),
            "address_line2": validated.get("address_line2",""),
            "postal_code": validated.get("postal_code",""),
            "city": validated.get("city",""),
            "country": validated.get("country",""),
            "phone": validated.get("phone",""),
        }

        # match an exact-ish address already stored
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

        # otherwise create new Address
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
            street_name=validated.get("street_name",""),
            house_number=validated.get("house_number",""),
        )
        # first address becomes default
        if not Address.objects.filter(user=user).exclude(pk=addr.pk).exists():
            addr.set_default_for_user()
        return addr

    def create(self, validated):
        req_user = getattr(self.context["request"], "user", None)
        is_auth = bool(req_user and req_user.is_authenticated and getattr(req_user, "is_active", True))

        items_in = validated["items"]
        clean_items, subtotal = self._calc_prices(items_in)

        coupon_code = (validated.get("coupon_code") or "").strip()
        coupon_obj, discount, _ = self._apply_coupon(req_user, items_in, subtotal, coupon_code)

        shipping_fee = Decimal("0.00")  # placeholder (you can calculate later by country/weight)
        total = (subtotal - discount + shipping_fee).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if total < 0:
            total = Decimal("0.00")

        # address snapshot ALWAYS saved on order
        snap_fn = validated.get("first_name", "")
        snap_ln = validated.get("last_name", "")
        snap_country = validated.get("country", "")
        snap_addr1 = validated.get("address_line1") or (
            f"{validated.get('street_name','')} {validated.get('house_number','')}".strip()
        )
        snap_addr2 = validated.get("address_line2","")
        snap_zip = validated.get("postal_code","")
        snap_city = validated.get("city","")
        snap_phone = validated.get("phone","")
        snap_street_name = validated.get("street_name","")
        snap_house_number = validated.get("house_number","")

        # if logged-in -> we also keep FK to Address
        addr_obj = self._resolve_address_for_user(req_user, validated) if is_auth else None

        with transaction.atomic():
            order = Order.objects.create(
                user=req_user if is_auth else None,
                email=validated["email"],
                status="pending",
                payment_method=validated.get("payment_method") or "card",
                subtotal=subtotal.quantize(Decimal("0.01")),
                shipping_fee=shipping_fee,
                total_price=total,
                shipping_address=addr_obj,
                coupon=coupon_obj,
                discount_amount=discount,
                installments_months=validated.get("months"),

                ship_first_name=snap_fn,
                ship_last_name=snap_ln,
                ship_line1=snap_addr1,
                ship_line2=snap_addr2,
                ship_postal=snap_zip,
                ship_city=snap_city,
                ship_country=snap_country,
                ship_phone=snap_phone,
                ship_street_name=snap_street_name,
                ship_house_number=snap_house_number,
            )
            for p, qty, unit in clean_items:
                OrderItem.objects.create(order=order, product=p, quantity=qty, unit_price=unit)

        return order
        
# ----------------- Order list/detail (legacy quick use) -----------------

class OrderListItemSerializer(serializers.ModelSerializer):
    product = ProductMiniSerializer()
    class Meta:
        model = Order
        fields = ['id', 'quantity', 'total_price', 'product']

class OrderDetailSerializer(serializers.ModelSerializer):
    product = ProductMiniSerializer()
    shipping_address = AddressSerializer()
    class Meta:
        model = Order
        fields = [
            'id', 'created_at', 'status', 'quantity', 'total_price',
            'product', 'shipping_address'
        ]

# ----------------- Coupon preview input -----------------

class CouponPreviewIn(serializers.Serializer):
    code  = serializers.CharField()
    items = OrderItemIn(many=True)

# ----------------- Promotions -----------------

class PromotionSerializer(serializers.ModelSerializer):
    code = serializers.SerializerMethodField()
    image = serializers.SerializerMethodField()
    remaining_seconds = serializers.SerializerMethodField()

    class Meta:
        model = PromotionBanner
        fields = [
            "id", "headline", "subheadline", "code",
            "start_at", "end_at", "remaining_seconds",
            "bg_color", "text_color", "link_url", "image"
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

# ----------------- Reviews -----------------

class ReviewSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.title_en", read_only=True)
    image = serializers.SerializerMethodField()
    author_avatar_url = serializers.SerializerMethodField()
    user_name = serializers.SerializerMethodField()
    is_owner = serializers.SerializerMethodField()

    class Meta:
        model = Review
        fields = [
            "id", "product", "product_name",
            "rating", "message", "image", "author_avatar_url",
            "author_name",
            "created_at",
            "user_name", "is_owner",
        ]
        read_only_fields = ["product", "product_name", "user_name", "is_owner", "author_avatar_url"]

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
        fields = ["rating", "message", "image", "author_name", "author_avatar", "user"]

    def validate_rating(self, v):
        if v < 1 or v > 5:
            raise serializers.ValidationError("Rating must be between 1 and 5.")
        return v

 
 # ----------------- Stock Alert -----------------
class OnDemandOrderRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = OnDemandOrderRequest
        fields = ["id", "product", "name", "email", "quantity", "message", "created_at"]
        read_only_fields = ["id", "created_at"]

    def validate(self, attrs):
        qty = int(attrs.get("quantity") or 0)
        if qty < 1:
            raise serializers.ValidationError({"quantity": "Must be at least 1."})
        # Ensure product exists (and is active if you have such a flag)
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