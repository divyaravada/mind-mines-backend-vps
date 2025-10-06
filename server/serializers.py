#server/serializers.py
from rest_framework import serializers
from django.utils import timezone
from .models import Promotion, ReturnRequest, NaturalStoneImage, OrderItem, User,Profile,Wishlist,NaturalStone,StoneEnquiry, Product,ProductMedia, ContactMessage, NewsletterSubscriber,Address, Order, Review
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes 
from django.contrib.auth.password_validation import validate_password
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.conf import settings
from django.template.loader import render_to_string, TemplateDoesNotExist
from django.core.mail import EmailMultiAlternatives
from django.contrib.auth.tokens import default_token_generator
from decimal import Decimal, ROUND_HALF_UP

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
        # username on User
        if "username" in validated_data:
            instance.username = validated_data["username"]
            instance.save(update_fields=["username"])

        # profile fields
        prof = instance.profile
        for f in ["full_name", "mobile", "image"]:
            if f in validated_data:
                setattr(prof, f, validated_data[f])
        prof.save()
        return instance

class AddressSerializer(serializers.ModelSerializer):
    # Map UI -> model
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

        # ←← KEY FIX: remove is_default from kwargs to avoid duplicate
        make_default = bool(validated_data.pop("is_default", False))

        # Optionally synthesize address_line1 from legacy inputs if missing
        if not validated_data.get("address_line1"):
            hn = self.initial_data.get("house_number") or ""
            sn = self.initial_data.get("street_name") or ""
            synth = " ".join([sn, hn]).strip()
            if synth:
                validated_data["address_line1"] = synth

        addr = Address.objects.create(user=user, **validated_data)

        # First address or explicit checkbox becomes default
        is_first = not Address.objects.filter(user=user).exclude(pk=addr.pk).exists()
        if is_first or make_default:
            addr.set_default_for_user()

        self._sync_full_name(addr)
        return addr

    def update(self, instance, validated_data):
        # ←← KEY FIX: handle default toggle separately
        make_default = bool(validated_data.pop("is_default", False))

        for k, v in validated_data.items():
            setattr(instance, k, v)
        instance.save()

        if make_default:
            instance.set_default_for_user()

        self._sync_full_name(instance)
        return instance

class WishlistSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product", read_only=True)
    class Meta:
        model = Wishlist
        fields = ["id", "product", "product_name"]

class MyTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["full_name"] = getattr(user.profile, "full_name", "")
        token["username"] = user.username or ""
        token["email"] = user.email
        token["verified"] = getattr(user.profile, "verified", False)
        return token

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
            username=validated_data["username"],
            email=validated_data["email"],
            is_active=False,   # user must verify
        )
        user.set_password(validated_data["password"])
        user.save()

        # 🔑 Generate verification token & link (built-in generator)
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        base = getattr(settings, "FRONTEND_URL", "http://localhost:5173").rstrip("/")
        verification_link = f"{base}/verify-email/{uid}/{token}/"

        # Compose email
        subject = "Verify your MindMines account"
        text_body = f"Please click the link to verify your email:\n{verification_link}"
        from_email = settings.EMAIL_HOST_USER  # match SMTP user

        # 📧 Send HTML email (won’t crash server if SMTP fails)
        html_body = None
        try:
            html_body = render_to_string("emails/verification_email.html", {
                "user": user,
                "verification_link": verification_link,
            })
        except TemplateDoesNotExist:
            pass  # fall back to plain text

        email = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=from_email,
            to=[user.email],
        )
        if html_body:
            email.attach_alternative(html_body, "text/html")

        # In DEBUG raise errors so you see them; in prod, fail silently
        email.send(fail_silently=not settings.DEBUG)

        return user


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

class ProductMediaSerializer(serializers.ModelSerializer):
    image = serializers.SerializerMethodField()
    video = serializers.SerializerMethodField()

    class Meta:
        model = ProductMedia
        fields = ["image", "video", "caption", "order", "media_type"]

    def _abs(self, request, url: str) -> str:
        if not url:
            return ""
        if request and not str(url).startswith("http"):
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
    vat_price = serializers.SerializerMethodField()
    stock_status = serializers.SerializerMethodField()
    reviews_count = serializers.SerializerMethodField()
    average_rating = serializers.SerializerMethodField()

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
            "type_of_stone", "material", "color", "stone_color_image",
            "quantity", "dimensions", "weight", "category", "price",
            "key", "image",
            "media", "vat_price", "stock_status", "reviews_count", "average_rating",
        ]

    def _abs(self, request, url: str) -> str:
        if not url:
            return ""
        if request and not str(url).startswith("http"):
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

    def get_vat_price(self, obj):
        return obj.vat_included_price()

    def get_stock_status(self, obj):
        return obj.stock_message()

    def get_reviews_count(self, obj):
        return getattr(obj, "reviews_count", 0) or 0

    def get_average_rating(self, obj):
        avg = getattr(obj, "average_rating", None)
        return round(float(avg), 1) if avg is not None else None

class ProductMiniSerializer(serializers.ModelSerializer):
    image = serializers.SerializerMethodField()
    price = serializers.DecimalField(max_digits=10, decimal_places=2)
    class Meta:
        model = Product
        fields = ["id", "title_en", "title_de", "image", "price"]

    def _abs(self, request, url: str) -> str:
        if not url:
            return ""
        if request and not str(url).startswith("http"):
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

class ContactMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactMessage
        fields = ["id", "name", "email", "message", "sent_at"]

class NewsletterSubscriberSerializer(serializers.ModelSerializer):
    email = serializers.EmailField()

    class Meta:
        model = NewsletterSubscriber
        fields = ["id", "email", "subscribed_at"]

    def validate_email(self, value: str) -> str:
        # normalize
        return value.strip().lower()

class OrderSerializer(serializers.ModelSerializer):
    product = ProductSerializer()
    class Meta:
        model = Order
        fields = ["id", "product", "quantity", "created_at"]

# ✅ Order items + order serializers
class OrderItemReadSerializer(serializers.ModelSerializer):
    product = ProductMiniSerializer(read_only=True)

    class Meta:
        model = OrderItem
        fields = ["id", "product", "quantity", "unit_price"]

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
            "id", "number", "status", "payment_method","payment_method_label",
            "subtotal","discount_amount", "shipping_fee", "total_price",
            "created_at", "updated_at", "tracking_number",
            "items", "invoice_pdf", "is_returnable", "return_deadline",
            "shipping_address",    "coupon_code",
        ]

    def get_payment_method_label(self, obj):
        # human label for choices
        try:
            return obj.get_payment_method_display()
        except Exception:
            return (obj.payment_method or "").title()

    def get_invoice_pdf(self, obj):
        try:
            if hasattr(obj, "invoice") and obj.invoice and obj.invoice.pdf:
                return obj.invoice.pdf.url
        except Exception:
            pass
        return None
    
    def get_is_returnable(self, obj):
        # simple 15-day window from created_at (adjust if you add delivered_at)
        from datetime import timedelta
        from django.utils.timezone import now
        if obj.status in ("cancelled",):
            return False
        return now() <= (obj.created_at + timedelta(days=1))

    def get_return_deadline(self, obj):
        from datetime import timedelta
        return (obj.created_at + timedelta(days=1)).isoformat()
    
    def get_coupon_code(self, obj):
        return getattr(obj.coupon, "code", None)
    
# ✅ Return request
class ReturnRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReturnRequest
        fields = ("id", "request_type", "message", "status", "created_at")


# ✅ Order create (for building a pending order from cart)
class OrderItemIn(serializers.Serializer):
    product_id = serializers.IntegerField()
    qty = serializers.IntegerField(min_value=1)

class OrderCreateSerializer(serializers.Serializer):
    items = OrderItemIn(many=True)
    payment_method = serializers.ChoiceField(
        choices=[c[0] for c in Order._meta.get_field("payment_method").choices],
        default="card",
    )
    coupon_code = serializers.CharField(allow_blank=True, required=False)
    address_id = serializers.IntegerField(required=False)
    first_name = serializers.CharField(allow_blank=True, required=False)
    last_name  = serializers.CharField(allow_blank=True, required=False)
    address_line1 = serializers.CharField(allow_blank=True, required=False)
    address_line2 = serializers.CharField(allow_blank=True, required=False)
    postal_code = serializers.CharField(allow_blank=True, required=False)
    city = serializers.CharField(allow_blank=True, required=False)
    country = serializers.CharField(allow_blank=True, required=False)
    phone = serializers.CharField(allow_blank=True, required=False)

    # Legacy (optional)
    house_number = serializers.CharField(allow_blank=True, required=False)
    street_name  = serializers.CharField(allow_blank=True, required=False)

    def _resolve_address(self, user, validated):
        aid = validated.get("address_id")
        if aid:
            a = Address.objects.filter(user=user, id=aid).first()
            if a:
                return a

        # match by signature
        sig = _addr_signature_dict(validated)
        if any(sig):
            for a in Address.objects.filter(user=user):
                if _addr_signature_model(a) == sig:
                    return a

        # if mostly empty and user has a default -> reuse default
        mostly_empty = not any([validated.get("address_line1"),
                                validated.get("street_name"),
                                validated.get("postal_code"),
                                validated.get("city")])
        if mostly_empty:
            default = Address.objects.filter(user=user, is_default=True).first()
            if default:
                return default

        # create new
        line1 = validated.get("address_line1") or (" ".join([
            validated.get("street_name",""), validated.get("house_number","")
        ])).strip()

        addr = Address.objects.create(
            user=user,
            first_name=validated.get("first_name",""),
            last_name=validated.get("last_name",""),
            address_line1=line1,
            apartment=validated.get("address_line2",""),
            postal_code=validated.get("postal_code",""),
            city=validated.get("city",""),
            country=validated.get("country",""),
            phone=validated.get("phone",""),
            street_name=validated.get("street_name",""),
            house_number=validated.get("house_number",""),
        )
        # first address becomes default
        if not Address.objects.filter(user=user).exclude(pk=addr.pk).exists():
            addr.set_default_for_user()
        return addr

    def _apply_coupon(self, user, items, subtotal, code):
        if not code:
            return None, Decimal("0.00"), None
        from server.models import Coupon
        coup = Coupon.objects.filter(code__iexact=str(code).strip()).first()
        if not coup:
            raise serializers.ValidationError({"coupon_code": "Coupon not found."})
        ok, reason = coup.can_apply(user, items, subtotal)
        if not ok:
            raise serializers.ValidationError({"coupon_code": reason})
        disc = coup.discount_amount(user, items, subtotal)
        return coup, disc, None

    def create(self, validated):
        user = self.context["request"].user
        # create/update address
        addr = self._resolve_address(user, validated)
        

        # build order items
        items_in = validated["items"]
        order_items, subtotal = [], Decimal("0.00")
        for it in items_in:
            product = Product.objects.get(pk=it["product_id"])
            qty = int(it["qty"]) 
            oi = OrderItem.objects.create(product=product, quantity=qty, unit_price=Product.vat_included_price(product))
            order_items.append(oi)
            subtotal += Product.vat_included_price(product) * qty

        coupon_code = validated.get("coupon_code", "").strip()
        coupon_obj, discount, _ = self._apply_coupon(user, items_in, subtotal, coupon_code)
        
        shipping_fee = Decimal("0.00")  
        total = (subtotal + shipping_fee).quantize(Decimal("0.01"))

        total = (subtotal - discount + shipping_fee).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if total < 0:
            total = Decimal("0.00")

        
        order = Order.objects.create(
            user=user,
            status="pending",
            payment_method=validated["payment_method"],
            subtotal=subtotal.quantize(Decimal("0.01")),
            shipping_fee=shipping_fee,
            total_price=total,
            shipping_address=addr,
            coupon=coupon_obj,
            discount_amount=discount
        )
        order.items.add(*order_items)
        return order

class OrderListItemSerializer(serializers.ModelSerializer):
    product = ProductMiniSerializer()
    class Meta:
        model = Order
        fields = ['id', 'created_at', 'status', 'quantity', 'total_price', 'product']

class OrderDetailSerializer(serializers.ModelSerializer):
    product = ProductMiniSerializer()
    shipping_address = AddressSerializer()
    class Meta:
        model = Order
        fields = [
            'id', 'created_at', 'status', 'quantity', 'total_price',
            'product', 'shipping_address'
        ]
 

#-------------- Coupon ---------------
class CouponPreviewIn(serializers.Serializer):
    code  = serializers.CharField()
    items = OrderItemIn(many=True)

#-----------Promotion---------------
class PromotionSerializer(serializers.ModelSerializer):
    code = serializers.SerializerMethodField()
    image = serializers.SerializerMethodField()
    remaining_seconds = serializers.SerializerMethodField()

    class Meta:
        model = Promotion
        fields = [
            "id", "headline", "subheadline", "code",
            "start_at", "end_at", "remaining_seconds",
            "bg_color", "text_color", "link_url", "image"
        ]

    def _abs(self, request, url: str) -> str:
        if not url:
            return ""
        if request and not str(url).startswith("http"):
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
        now = timezone.now()
        if obj.end_at <= now:
            return 0
        delta = obj.end_at - now
        return max(0, int(delta.total_seconds()))

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
            "author_name",        # expose what admin set (if any)
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
        # Prefer admin-provided override
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
    # Only admins may use `user` to attribute review to another account.
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