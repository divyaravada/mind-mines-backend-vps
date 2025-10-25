#server/serializers.py
from rest_framework import serializers
from django.utils import timezone
from django.conf import settings
from django.db import transaction
from decimal import Decimal
from django.contrib.auth.password_validation import validate_password
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.template.exceptions import TemplateDoesNotExist
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from datetime import timedelta, datetime


from .models import (
    User, Profile,
    Address,
    NaturalStone, NaturalStoneImage, StoneEnquiry,
    Product, ProductMedia,
    Wishlist,
    ContactMessage, NewsletterSubscriber,
    Order, OrderItem, Invoice,
    Review,
    Promotion, StockAlert,Coupon
)

# ---------------------- JWT Serializer ----------------------
class MyTokenObtainPairSerializer(TokenObtainPairSerializer):
    """
    Extend the JWT payload with a few user fields used by the frontend.
    """
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        # Safe extras (customize as you like)
        token["email"] = getattr(user, "email", "")
        token["username"] = getattr(user, "username", "") or ""
        token["full_name"] = getattr(getattr(user, "profile", None), "full_name", "")
        token["verified"] = bool(getattr(getattr(user, "profile", None), "verified", False))
        return token

# ---------------------- User / Profile ----------------------

class UserMinimalSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "email", "display_name")

class ProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = Profile
        fields = ("full_name", "mobile", "image", "verified",  'date_of_birth')
        read_only_fields = ("verified",)

class AccountSerializer(serializers.Serializer):
    email = serializers.EmailField(read_only=True)
    username = serializers.CharField(required=False, allow_blank=True)
    full_name = serializers.CharField(required=False, allow_blank=True)
    mobile = serializers.CharField(required=False, allow_blank=True)
    image = serializers.ImageField(required=False, allow_null=True)
    verified = serializers.BooleanField(read_only=True)
    date_of_birth = serializers.DateField(required=False, allow_null=True)

    def to_representation(self, instance):
        user = instance
        prof = getattr(user, "profile", None)
        return {
            "email": user.email,
            "username": user.username or "",
            "full_name": getattr(prof, "full_name", ""),
            "mobile": getattr(prof, "mobile", ""),
            "image": (prof.image.url if getattr(prof, "image", None) else None),
            "verified": getattr(prof, "verified", False),
            "date_of_birth": getattr(prof, "date_of_birth", None),
        }

    def update(self, instance, validated_data):
        if "username" in validated_data:
            instance.display_name = validated_data["username"]
            instance.save(update_fields=["username"])

        prof = instance.profile
        for f in ("full_name", "mobile", "image", "date_of_birth"):
            if f in validated_data:
                setattr(prof, f, validated_data[f])
        prof.save()
        return instance

class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=True, validators=[validate_password])
    password2 = serializers.CharField(write_only=True, required=True)
    # ✅ NEW (optional at signup)
    date_of_birth = serializers.DateField(required=False, allow_null=True)

    class Meta:
        model = User
        fields = ('email', 'username', 'password', 'password2', 'date_of_birth')  # ✅

    def validate(self, attrs):
        if attrs['password'] != attrs['password2']:
            raise serializers.ValidationError({"password": "Password fields didn't match."})
        return attrs

    def create(self, validated_data):
        dob = validated_data.pop("date_of_birth", None)  # ✅
        user = User(
            username=validated_data.get("username"),
            email=validated_data["email"],
            is_active=False,   # verify via email
        )
        user.set_password(validated_data["password"])
        user.save()

        # ✅ store DOB on profile if provided
        prof = getattr(user, "profile", None)
        if prof and dob:
            prof.date_of_birth = dob
            prof.save(update_fields=["date_of_birth"])

        # (verification email logic unchanged) ...
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        base = getattr(settings, "FRONTEND_URL", "https://mindmines.eu").rstrip("/")
        verification_link = f"{base}/verify-email/{uid}/{token}/"

        subject = "Verify your MindMines account"
        text_body = f"Please click the link to verify your email:\n{verification_link}"
        from_email = settings.EMAIL_HOST_USER

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
# ---------------------- Addresses ----------------------

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
        read_only_fields = ("created_at", "updated_at")

    def _sync_full_name(self, addr):
        full = " ".join(filter(None, [addr.first_name, addr.last_name])).strip()
        if full and addr.full_name != full:
            addr.full_name = full
            addr.save(update_fields=["full_name"])

    def create(self, validated_data):
        user = self.context["request"].user
        make_default = bool(validated_data.pop("is_default", False))
        if not validated_data.get("address_line1"):
            # synthesize from legacy
            hn = self.initial_data.get("house_number") or ""
            sn = self.initial_data.get("street_name") or ""
            synth = " ".join([sn, hn]).strip()
            if synth:
                validated_data["address_line1"] = synth
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
        instance.save()
        if make_default:
            instance.set_default_for_user()
        self._sync_full_name(instance)
        return instance
       

# ---------------------- Natural Stones ----------------------

class NaturalStoneImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = NaturalStoneImage
        fields = ("id", "image")

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
            "image", "images",
        ]

class StoneEnquirySerializer(serializers.ModelSerializer):
    class Meta:
        model = StoneEnquiry
        fields = ("id", "stone_type", "name", "email", "message", "created_at")

# ---------------------- Products & Media ----------------------

class ProductMiniSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField()
    title_en = serializers.CharField()
    title_de = serializers.CharField()
    image = serializers.SerializerMethodField()
    thumbnail_url = serializers.SerializerMethodField()
    price = serializers.DecimalField(max_digits=10, decimal_places=2)

    def _abs(self, request, url: str) -> str:
        if not url:
            return ""
        if request and not str(url).startswith("http"):
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

class ProductMediaSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()
    video_url = serializers.SerializerMethodField()

    class Meta:
        model = ProductMedia
        fields = ("media_type", "image_url", "video_url", "caption", "order")

    def _abs(self, request, url: str) -> str:
        if not url:
            return ""
        if request and not str(url).startswith(("http://", "https://")):
            return request.build_absolute_uri(url)
        return url

    def get_image_url(self, obj):
        request = self.context.get("request")
        try:
            return self._abs(request, obj.image.url) if obj.image else ""
        except Exception:
            return ""

    def get_video_url(self, obj):
        request = self.context.get("request")
        try:
            return self._abs(request, obj.video.url) if obj.video else ""
        except Exception:
            return ""

class ProductListSerializer(serializers.ModelSerializer):
    media = ProductMediaSerializer(many=True, read_only=True)
    image_url = serializers.SerializerMethodField()
    is_discounted = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = (
            "id", "key",
            "title_en", "title_de",
            "category",
            "price_gross", "discount_gross", "sale_price_gross",
            "stock",
            "image_url",
            "is_discounted",
            "media",
        )

    def get_image_url(self, obj):
        request = self.context.get("request")
        try:
            return request.build_absolute_uri(obj.image.url) if (obj.image and request) else (obj.image.url if obj.image else "")
        except Exception:
            return ""

    def get_is_discounted(self, obj):
        return bool(obj.discount_gross and obj.sale_price_gross < obj.price_gross)

class ProductDetailSerializer(serializers.ModelSerializer):
    media = ProductMediaSerializer(many=True, read_only=True)
    image_url = serializers.SerializerMethodField()
    is_discounted = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = (
            "id", "key",
            "title_en", "title_de",
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
            "type_of_stone", "material", "color", "stone_color_image",
            "dimensions", "weight", "category",
            "price_gross", "discount_gross", "sale_price_gross", "vat_rate",
            "stock",
            "image_url",
            "is_discounted",
            "media",
        )

    def get_image_url(self, obj):
        request = self.context.get("request")
        try:
            return request.build_absolute_uri(obj.image.url) if (obj.image and request) else (obj.image.url if obj.image else "")
        except Exception:
            return ""

    def get_is_discounted(self, obj):
        return bool(obj.discount_gross and obj.sale_price_gross < obj.price_gross)

# ---------------------- Wishlist ----------------------

class WishlistSerializer(serializers.ModelSerializer):
    product_title = serializers.CharField(source="product.title_en", read_only=True)
    class Meta:
        model = Wishlist
        fields = ("id", "product", "product_title", "created_at")

# ---------------------- Contact / Newsletter ----------------------

class ContactMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactMessage
        fields = ("id", "name", "email", "message", "sent_at")

    def validate(self, attrs):
        if len(attrs.get("message", "").strip()) < 10:
            raise serializers.ValidationError("Message is too short.")
        return attrs

class NewsletterSubscriberSerializer(serializers.ModelSerializer):
    email = serializers.EmailField()
    class Meta:
        model = NewsletterSubscriber
        fields = ("id", "email", "subscribed_at")
    def validate_email(self, value: str) -> str:
        return value.strip().lower()


# ---------------------- Orders (CREATE) ----------------------
class OrderItemReadSerializer(serializers.ModelSerializer):
    product = ProductMiniSerializer()
    line_total = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = ["id", "product", "quantity", "unit_price", "line_total"]

    def get_line_total(self, obj):
        try:
            return obj.line_total()
        except Exception:
            return Decimal("0.00")

# --- Full order read serializer used by views (the one missing) ---
class OrderReadSerializer(serializers.ModelSerializer):
    items = OrderItemReadSerializer(many=True, read_only=True)
    invoice_pdf = serializers.SerializerMethodField()
    shipping_address = AddressSerializer(read_only=True)
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
            "shipping_address", "coupon_code",
        ]

    def get_payment_method_label(self, obj):
        try:
            return obj.get_payment_method_display()
        except Exception:
            return (obj.payment_method or "").title()

    def get_invoice_pdf(self, obj):
        # Prefer related Invoice model if present
        try:
            inv = getattr(obj, "invoice", None)
            f = getattr(inv, "pdf", None)
            if f and getattr(f, "url", None):
                req = self.context.get("request")
                url = f.url
                return req.build_absolute_uri(url) if req and not url.startswith(("http://", "https://")) else url
        except Exception:
            pass
        # Legacy FileField fallback on Order
        try:
            f = getattr(obj, "invoice_pdf", None)
            if f and getattr(f, "url", None):
                req = self.context.get("request")
                url = f.url
                return req.build_absolute_uri(url) if req and not url.startswith(("http://", "https://")) else url
        except Exception:
            pass
        return None

    def get_is_returnable(self, obj):
        # If you have delivered_at logic on the model, prefer its property; otherwise keep the 1-day fallback
        if obj.status in ("cancelled",):
            return False
        return now() <= (obj.created_at + timedelta(days=1))

    def get_return_deadline(self, obj):
        return (obj.created_at + timedelta(days=1)).isoformat()

    def get_coupon_code(self, obj):
        return getattr(getattr(obj, "coupon", None), "code", None)
    
class OrderItemInSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    qty = serializers.IntegerField(min_value=1)

class CouponPreviewIn(serializers.Serializer):
    code  = serializers.CharField()
    items = OrderItemInSerializer(many=True)

class AddressSnapshotSerializer(serializers.Serializer):
    first_name = serializers.CharField(allow_blank=True, required=False)
    last_name  = serializers.CharField(allow_blank=True, required=False)
    address_line1 = serializers.CharField(allow_blank=True, required=False)
    address_line2 = serializers.CharField(allow_blank=True, required=False)
    postal_code = serializers.CharField(allow_blank=True, required=False)
    city = serializers.CharField(allow_blank=True, required=False)
    country = serializers.CharField(allow_blank=True, required=False)
    phone = serializers.CharField(allow_blank=True, required=False)

class OrderCreateSerializer(serializers.Serializer):
    # Guests must provide email. Logged-in users can omit.
    email = serializers.EmailField(required=False, allow_blank=True)

    # Items
    items = OrderItemInSerializer(many=True)

    # Payment
    payment_method = serializers.ChoiceField(
        choices=[c[0] for c in Order.PAYMENT_METHODS],
        default="stripe_card"
    )
    installments_months = serializers.IntegerField(required=False, min_value=2, max_value=24)

    # Address snapshots (what the user typed at checkout)
    shipping_address = AddressSnapshotSerializer()
    billing_address  = AddressSnapshotSerializer()

    # Optional coupon
    coupon_code = serializers.CharField(allow_blank=True, required=False)

    remarks = serializers.CharField(allow_blank=True, required=False)

    def validate(self, attrs):
        req = self.context.get("request")
        user = getattr(req, "user", None)
        if not (user and user.is_authenticated):
            if not (attrs.get("email") or "").strip():
                raise serializers.ValidationError({"email": "Email is required for guest checkout."})
        # If SEPA, you can enforce country == DE here if you want.
        return attrs

    @transaction.atomic
    def create(self, validated):
        req = self.context.get("request")
        user = req.user if (req and req.user.is_authenticated) else None

        # 1) compute totals from product snapshot prices (gross)
        items_in = validated["items"]
        products = {}
        subtotal = Decimal("0.00")

        clean_items = []
        for it in items_in:
            pid = int(it["product_id"]); qty = int(it["qty"])
            if pid not in products:
                try:
                    products[pid] = Product.objects.get(pk=pid)
                except Product.DoesNotExist:
                    raise serializers.ValidationError({"items": f"Product {pid} not found"})
            p = products[pid]
            unit = (p.sale_price_gross or p.price_gross)
            clean_items.append((p, qty, unit, p.vat_rate))
            subtotal += (unit * qty)

        subtotal = subtotal.quantize(Decimal("0.01"))
        vat_rate = Decimal(settings.VAT_RATE) if hasattr(settings, "VAT_RATE") else Decimal("0.19")
        vat_amount = (subtotal - (subtotal / (Decimal("1.00") + vat_rate))).quantize(Decimal("0.01"))
        shipping_fee = Decimal("0.00")  # add rules if needed now
        total = (subtotal + shipping_fee).quantize(Decimal("0.01"))

        # 2) coupon (if you want: apply only to eligible items using your Coupon helpers)
        coupon_code = (validated.get("coupon_code") or "").strip()
        coupon_obj = None
        discount = Decimal("0.00")
        if coupon_code:
            c = Coupon.objects.filter(code__iexact=coupon_code).first()
            if not c:
                raise serializers.ValidationError({"coupon_code": "Coupon not found."})
            # Here you can reuse your coupon.can_apply / discount_amount
            ok, reason = c.can_apply(user, items_in, subtotal)
            if not ok:
                raise serializers.ValidationError({"coupon_code": reason})
            discount = c.discount_amount(user, items_in, subtotal)
            total = max(Decimal("0.00"), (total - discount)).quantize(Decimal("0.01"))
            coupon_obj = c

        # 3) email & snapshots
        email = (user.email if user else validated.get("email"))
        if not email:
            raise serializers.ValidationError({"email": "Email is required."})

        ship = validated["shipping_address"]
        bill = validated["billing_address"]

        # 4) create order
        order = Order.objects.create(
            user=user,
            email=email,
            guest_name=" ".join(filter(None, [ship.get("first_name",""), ship.get("last_name","")])),
            shipping_address_json=ship,
            billing_address_json=bill,
            order_number="",  # will be assigned in model.save()
            currency="eur",
            subtotal_gross=subtotal,
            vat_amount=vat_amount,
            shipping_fee=shipping_fee,
            total_gross=total,
            payment_method=validated.get("payment_method") or "stripe_card",
            installments_months=validated.get("installments_months"),
            remarks=validated.get("remarks",""),
        )

        # 5) items snapshot
        for p, qty, unit, vatr in clean_items:
            OrderItem.objects.create(
                order=order, product=p, quantity=qty,
                unit_price_gross=unit, vat_rate=vatr
            )

        # 6) attach coupon redemption later (after payment success) if desired
        return order
    
# ---------------------- Guest lookup ----------------------

class GuestOrderLookupSerializer(serializers.Serializer):
    email = serializers.EmailField()
    order_number = serializers.CharField(max_length=20)

# ---------------------- Promotions / Stock Alerts ----------------------

class PromotionSerializer(serializers.ModelSerializer):
    code = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    remaining_seconds = serializers.SerializerMethodField()

    class Meta:
        model = Promotion
        fields = (
            "id", "headline", "subheadline",
            "code", "start_at", "end_at", "remaining_seconds",
            "bg_color", "text_color", "link_url", "image_url"
        )

    def _abs(self, request, url: str) -> str:
        if not url: return ""
        if request and not url.startswith(("http://","https://")):
            return request.build_absolute_uri(url)
        return url

    def get_code(self, obj): return obj.current_code()

    def get_image_url(self, obj):
        try:
            url = obj.image.url if obj.image else ""
            return self._abs(self.context.get("request"), url)
        except Exception:
            return ""

    def get_remaining_seconds(self, obj):
        now = timezone.now()
        return max(0, int((obj.end_at - now).total_seconds())) if obj.end_at > now else 0

class StockAlertCreateSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    email = serializers.EmailField()

    def validate_email(self, v):
        return (v or "").strip().lower()

    def create(self, validated):
        alert, created = StockAlert.objects.get_or_create(
            product=validated["product"], email=validated["email"],
            defaults={"is_active": True}
        )
        if not created and not alert.is_active:
            alert.is_active = True
            alert.notified_at = None
            alert.save(update_fields=["is_active", "notified_at"])
        return alert

# ---------------------- Reviews ----------------------

class ReviewSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.title_en", read_only=True)
    image_url = serializers.SerializerMethodField()
    author_avatar_url = serializers.SerializerMethodField()
    user_display = serializers.SerializerMethodField()
    is_owner = serializers.SerializerMethodField()

    class Meta:
        model = Review
        fields = (
            "id", "product", "product_name",
            "rating", "message",
            "image_url", "author_avatar_url",
            "author_name", "created_at",
            "user_display", "is_owner",
        )

    def _abs(self, request, f):
        if not f: return None
        try:
            url = f.url
        except Exception:
            return None
        return request.build_absolute_uri(url) if request and not url.startswith(("http://","https://")) else url

    def get_image_url(self, obj):
        return self._abs(self.context.get("request"), obj.image)

    def get_author_avatar_url(self, obj):
        return self._abs(self.context.get("request"), obj.author_avatar)

    def get_user_display(self, obj):
        if obj.author_name:
            return obj.author_name
        u = obj.user
        if hasattr(u, "profile") and u.profile.full_name:
            return u.profile.full_name
        return u.display_name or (u.email.split("@")[0] if u.email else "User")

    def get_is_owner(self, obj):
        req = self.context.get("request")
        return bool(req and req.user.is_authenticated and obj.user_id == req.user.id)

class ReviewWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Review
        fields = ("rating", "message", "image", "author_name", "author_avatar")

    def validate_rating(self, v):
        if v < 1 or v > 5:
            raise serializers.ValidationError("Rating must be between 1 and 5.")
        return v





