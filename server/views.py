# server/views.py
import json, re, requests as http
import logging
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import List, Optional

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMultiAlternatives, send_mail
from django.db import transaction
from django.db.models import Q, Avg, Count
from django.db.models.functions import Lower
from django.http import JsonResponse, Http404, HttpResponse, FileResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string, TemplateDoesNotExist
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.timezone import make_aware
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from itsdangerous import TimestampSigner

from rest_framework import generics, permissions, serializers, filters, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import AllowAny, IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import RefreshToken

from google.oauth2 import id_token
from google.auth.transport.requests import Request as GoogleRequest

from dateutil.relativedelta import relativedelta

# --- local imports (models/serializers/utils) ---
from .models import (
    Address, Product, Review, Order, Wishlist,
    OnDemandOrderRequest, NewsletterSubscriber, ContactMessage,
    PromotionBanner, StockAlert, ReturnRequest, ReturnAttachment,
    NaturalStone, NaturalStoneImage
)
from .serializers import (
    MyTokenObtainPairSerializer, ProfileSerializer, RegisterSerializer, AccountSerializer,
    AddressSerializer, NaturalStoneSerializer, StoneEnquirySerializer,
    ProductSerializer, ProductMiniSerializer,
    ReviewWriteSerializer, ReviewSerializer,
    ContactMessageSerializer, NewsletterSubscriberSerializer,
    WishlistSerializer,
    OrderReadSerializer, OrderCreateSerializer,
    PromotionSerializer, CouponPreviewIn,
    StockAlertCreateSerializer, OnDemandOrderRequestSerializer,
)
from .pagination import StandardResultsSetPagination
from .utils.invoices import generate_invoice_pdf
from .utils.emails import (
    send_invoice_email,
    send_cancel_request_emails,
    send_return_request_emails,
    send_stock_alert_email,
)

import stripe
try:
    stripe_error = stripe.error
except AttributeError:
    from stripe import _error as stripe_error

# -----------------------------------------------------------------------------
# Setup / constants
# -----------------------------------------------------------------------------
logger = logging.getLogger(__name__)
User = get_user_model()

def _safe(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception:
        return None

# 2 hours signed token window for guest invoice links
SIGNER_SALT = "invoice-public-download-v1"
INVOICE_TOKEN_MAX_AGE = 60 * 60 * 2

def _get_signer():
    """TimestampSigner tied to SECRET_KEY and salt."""
    return TimestampSigner(key=settings.SECRET_KEY, salt=SIGNER_SALT)

# Returns window default: 15 days
RETURNS_WINDOW_DAYS = getattr(settings, "RETURNS_WINDOW_DAYS", 15)

# Stripe key
stripe.api_key = settings.STRIPE_SECRET_KEY

# Optional: background pipeline hooks
try:
    from server.tasks import post_payment_pipeline, fire_and_forget
except Exception:
    def fire_and_forget(func, *args, **kwargs):
        _safe(func, *args, **kwargs)
    def post_payment_pipeline(*_, **__):
        pass

# Helper: who to email for an order
def _order_to(order):
    return (getattr(order, "email", None)
            or getattr(getattr(order, "user", None), "email", None))

# Fallback bilingual sender for small notices
DEFAULT_FROM = getattr(settings, "EMAIL_HOST_USER", "no-reply@mindmines.eu")
def _send_bilingual(subject, to, en_html, de_html):
    text = f"ENGLISH\n{re.sub('<[^<]+?>','',en_html)}\n\nDEUTSCH\n{re.sub('<[^<]+?>','',de_html)}"
    html = (
        "<div style='font-family:sans-serif'>"
        "<h3>ENGLISH</h3>" + en_html + "<hr/><h3>DEUTSCH</h3>" + de_html + "</div>"
    )
    msg = EmailMultiAlternatives(subject=subject, body=text, from_email=DEFAULT_FROM, to=[to])
    msg.attach_alternative(html, "text/html")
    msg.send(fail_silently=not settings.DEBUG)

# Build signed guest invoice download URL
def build_guest_invoice_download_url(order) -> Optional[str]:
    inv = getattr(order, "invoice", None)
    if not inv or not inv.pdf:
        return None
    payload = {"order_id": order.id, "invoice": inv.number}
    signer = _get_signer()
    token = signer.sign(json.dumps(payload))
    base = getattr(settings, "FRONTEND_URL", "https://mindmines.eu").rstrip("/")
    api_base = getattr(settings, "API_BASE_PATH", "/api").rstrip("/")
    return f"{base}{api_base}/guest/invoice/download/?token={token}"

# -----------------------------------------------------------------------------
# Auth
# -----------------------------------------------------------------------------
class MyTokenObtainPairView(TokenObtainPairView):
    serializer_class = MyTokenObtainPairSerializer

class UserProfileAPIView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        return Response(ProfileSerializer(request.user.profile).data)
    def put(self, request):
        ser = ProfileSerializer(request.user.profile, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)

class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    permission_classes = [AllowAny]
    serializer_class = RegisterSerializer

class IsOwnerOrAdmin(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        return bool(request.user and (obj.user_id == request.user.id or request.user.is_staff))

# -----------------------------------------------------------------------------
# Account
# -----------------------------------------------------------------------------
class AccountAPIView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    def get(self, request):
        return Response(AccountSerializer(request.user).data)
    def put(self, request):
        ser = AccountSerializer(instance=request.user, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)

class AccountDeleteRequestView(APIView):
    permission_classes = [IsAuthenticated]
    def post(self, request):
        reason = (request.data.get("reason") or "").strip()
        if not reason:
            return Response({"detail": "Reason is required."}, status=400)
        user = request.user
        full_name = getattr(getattr(user, "profile", None), "full_name", "") or ""
        admin_to = getattr(settings, "ADMIN_EMAIL", None) or "info@mindmines.eu"
        subject = "Account deletion request"
        plain = (
            f"User requested account deletion.\n\n"
            f"Email: {user.email}\nUsername: {user.username or '-'}\n"
            f"Full name: {full_name or '-'}\nUser ID: {user.id}\n\nReason:\n{reason}\n"
        )
        html = (
            f"<p><strong>User requested account deletion.</strong></p>"
            f"<p><b>Email:</b> {user.email}<br/><b>Username:</b> {user.username or '-'}<br/>"
            f"<b>Full name:</b> {full_name or '-'}<br/><b>User ID:</b> {user.id}</p>"
            f"<p><b>Reason:</b><br/>{reason.replace(chr(10), '<br/>')}</p>"
        )
        email = EmailMultiAlternatives(
            subject=subject, body=plain,
            from_email=settings.EMAIL_HOST_USER,
            to=[admin_to], reply_to=[user.email],
        )
        email.attach_alternative(html, "text/html")
        email.send(fail_silently=False)
        return Response({"detail": "We received your request."}, status=200)

class AccountDeleteConfirmView(APIView):
    permission_classes = [IsAuthenticated]
    def post(self, request):
        user = request.user
        email_addr = user.email
        subj = "Your account has been deleted / Ihr Konto wurde gelöscht"
        en = ("We have deleted your MindMines account as requested.<br/>"
              "We're sorry to see you go. If this wasn't you, please contact support.")
        de = ("Wir haben Ihr MindMines-Konto wie gewünscht gelöscht.<br/>"
              "Schade, dass Sie gehen. Falls dies nicht von Ihnen veranlasst wurde, kontaktieren Sie bitte den Support.")
        user.delete()
        if email_addr:
            try:
                _send_bilingual(subj, email_addr, f"<p>{en}</p>", f"<p>{de}</p>")
            except Exception:
                logger.exception("Failed sending account deletion confirmation email")
        return Response({"detail": "Your account has been deleted."}, status=200)

# -----------------------------------------------------------------------------
# Addresses
# -----------------------------------------------------------------------------
class AddressListCreateAPIView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AddressSerializer
    def get_queryset(self):
        return Address.objects.filter(user=self.request.user).order_by("-is_default", "-updated_at", "-id")
    def perform_create(self, serializer):
        addr = serializer.save()
        if not Address.objects.filter(user=self.request.user).exclude(pk=addr.pk).exists():
            addr.set_default_for_user()
    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["request"] = self.request
        return ctx

class AddressDetailAPIView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = AddressSerializer
    def get_queryset(self):
        return Address.objects.filter(user=self.request.user)
    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["request"] = self.request
        return ctx
    def perform_destroy(self, instance):
        user = instance.user
        was_default = instance.is_default
        instance.delete()
        if was_default:
            new_default = Address.objects.filter(user=user).order_by("-updated_at", "-created_at").first()
            if new_default:
                new_default.set_default_for_user()

class AddressSetDefaultAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def post(self, request, pk):
        addr = get_object_or_404(Address, pk=pk, user=request.user)
        addr.set_default_for_user()
        return Response(AddressSerializer(addr, context={"request": request}).data)

# -----------------------------------------------------------------------------
# Social Login
# -----------------------------------------------------------------------------
class GoogleLoginAPIView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        token = request.data.get("token")
        if not token:
            return Response({"detail": "Missing token"}, status=400)
        try:
            idinfo = id_token.verify_oauth2_token(
                token, GoogleRequest(), settings.SOCIAL_AUTH_GOOGLE_OAUTH2_KEY,
            )
            if idinfo.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
                return Response({"detail": "Invalid issuer"}, status=400)
            email = idinfo["email"]
            username = email.split("@")[0]
            full_name = idinfo.get("name", "")
            user, created = User.objects.get_or_create(
                email=email, defaults={"username": username, "is_active": True},
            )
            if created and hasattr(user, "profile"):
                user.profile.full_name = full_name
                user.profile.save()
            refresh = RefreshToken.for_user(user)
            return Response({
                "refresh": str(refresh),
                "access": str(refresh.access_token),
                "user": {"id": user.id, "email": user.email, "username": user.username},
            })
        except ValueError as e:
            return Response({"detail": f"Google token invalid: {e}"}, status=400)
        except Exception as e:
            return Response({"detail": f"Auth error: {e}"}, status=400)

# -----------------------------------------------------------------------------
# Password reset
# -----------------------------------------------------------------------------
class CustomPasswordResetView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    def post(self, request):
        email = request.data.get("email")
        user = User.objects.filter(email=email).first()
        if user:
            uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            reset_url = f"{getattr(settings, 'FRONTEND_URL', 'https://mindmines.eu').rstrip('/')}/reset-password/{uidb64}/{token}/"
            message = render_to_string("registration/password_reset_email.html", {"reset_link": reset_url, "user": user})
            send_mail(
                subject="Reset Your Password",
                message="Click the link to reset your password.",
                html_message=message,
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", settings.EMAIL_HOST_USER),
                recipient_list=[email],
                fail_silently=True,
            )
        return Response({"detail": "Password reset e-mail has been sent."}, status=200)

class CustomPasswordResetConfirmView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    def post(self, request, uidb64, token):
        try:
            uid = urlsafe_base64_decode(uidb64).decode()
            user = User.objects.get(pk=uid)
        except Exception:
            return Response({"error": "Invalid link"}, status=400)
        if not default_token_generator.check_token(user, token):
            return Response({"error": "Invalid or expired token"}, status=400)
        password1 = request.data.get("new_password1")
        password2 = request.data.get("new_password2")
        if password1 != password2:
            return Response({"error": "Passwords do not match"}, status=400)
        user.set_password(password1)
        user.save()
        return Response({"detail": "Password has been reset successfully."}, status=200)

# -----------------------------------------------------------------------------
# Natural stones
# -----------------------------------------------------------------------------
class NaturalStoneListView(generics.ListAPIView):
    queryset = NaturalStone.objects.all()
    serializer_class = NaturalStoneSerializer
    permission_classes = [AllowAny]

class NaturalStoneDetailView(generics.RetrieveAPIView):
    queryset = NaturalStone.objects.all()
    serializer_class = NaturalStoneSerializer
    permission_classes = [AllowAny]
    lookup_field = "type"

class UploadStoneImagesView(APIView):
    parser_classes = [MultiPartParser]
    permission_classes = [IsAdminUser]
    def post(self, request, stone_id):
        stone = NaturalStone.objects.get(id=stone_id)
        for img in request.FILES.getlist("images"):
            NaturalStoneImage.objects.create(stone=stone, image=img)
        return Response({"detail": "Images uploaded."}, status=201)

class StoneEnquiryCreateView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        data = request.data.copy()
        user = request.user if request.user.is_authenticated else None
        if user:
            if not user.email:
                return Response({"email": ["Your account has no email on file."]}, status=400)
            data["email"] = user.email
        else:
            if not data.get("email"):
                return Response({"email": ["This field is required."]}, status=400)
        serializer = StoneEnquirySerializer(data=data)
        serializer.is_valid(raise_exception=True)
        enquiry = serializer.save()
        # user confirmation
        try:
            send_mail(
                subject="Thank you for your enquiry",
                message=(
                    f"Dear {enquiry.name},\n\n"
                    f"Thank you for reaching out regarding our natural stone: {enquiry.stone_type}.\n"
                    f"We will get back to you shortly.\n\n"
                    f"Best regards,\nMindMines Team"
                ),
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", settings.EMAIL_HOST_USER),
                recipient_list=[enquiry.email],
                fail_silently=False,
            )
        except Exception as e:
            logger.exception("Failed sending enquiry confirmation email: %s", e)
        # admin notify
        try:
            admin_to = getattr(settings, "ADMIN_EMAIL", None) or "info@mindmines.eu"
            send_mail(
                subject=f"New Natural Stone Enquiry: {enquiry.stone_type}",
                message=(
                    f"Stone: {enquiry.stone_type}\n"
                    f"Name: {enquiry.name}\n"
                    f"Email: {enquiry.email}\n"
                    f"Message:\n{enquiry.message}\n"
                ),
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", settings.EMAIL_HOST_USER),
                recipient_list=[admin_to],
                fail_silently=True,
            )
        except Exception:
            logger.exception("Failed sending admin enquiry email")
        return Response(StoneEnquirySerializer(enquiry).data, status=201)

class TestPostView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        return Response({"message": "POST request successful!"}, status=200)

# -----------------------------------------------------------------------------
# Products / Reviews
# -----------------------------------------------------------------------------
class ProductFilterMixin:
    def _parse_multi(self, key: str):
        q = self.request.query_params
        vals = q.getlist(key) or ([q.get(key)] if q.get(key) else [])
        out = []
        for v in vals:
            if not v or not isinstance(v, str):
                continue
            s = v.strip()
            if s.startswith('[') and s.endswith(']'):
                try:
                    arr = json.loads(s)
                    out.extend([str(x).strip().lower() for x in arr if str(x).strip()])
                    continue
                except Exception:
                    pass
            out.extend([p.strip().lower() for p in s.split(',') if p.strip()])
        return out

    def _to_decimal(self, val):
        if val is None:
            return None
        s = str(val).strip().replace('€', '').replace(',', '')
        if not s:
            return None
        try:
            return Decimal(s)
        except (InvalidOperation, ValueError, TypeError):
            return None

    def _parse_price_range(self):
        q = self.request.query_params
        min_in = q.get('min_price') or q.get('price_min') or q.get('price_gte')
        max_in = q.get('max_price') or q.get('price_max') or q.get('price_lte')
        rng = q.get('price') or q.get('range')
        if rng:
            s = str(rng).strip().lower()
            m = re.match(r'^\s*(\d+(?:\.\d{1,2})?)\s*-\s*(\d+(?:\.\d{1,2})?)\s*$', s)
            if m:
                min_in = min_in or m.group(1)
                max_in = max_in or m.group(2)
            m = re.match(r'^\s*(below|under|lt|lte)\s+(\d+(?:\.\d{1,2})?)\s*$', s)
            if m:
                max_in = max_in or m.group(2)
            m = re.match(r'^\s*(above|over|gt|gte)\s+(\d+(?:\.\d{1,2})?)\s*$', s)
            if m:
                min_in = min_in or m.group(2)
        lo = self._to_decimal(min_in)
        hi = self._to_decimal(max_in)
        if lo is not None and hi is not None and lo > hi:
            lo, hi = hi, lo
        return lo, hi

    def filtered_products_qs(self):
        qs = Product.objects.all()
        category = self.request.query_params.get("category")
        if category:
            qs = qs.filter(category=category)
        materials = self._parse_multi("material")
        if materials:
            qs = qs.filter(material__in=materials)
        colors = self._parse_multi("color")
        if colors:
            qs = qs.filter(color__in=colors)

        # filter on stored gross price
        min_price, max_price = self._parse_price_range()
        if min_price is not None:
            qs = qs.filter(price_gross__gte=min_price)
        if max_price is not None:
            qs = qs.filter(price_gross__lte=max_price)

        qs = qs.annotate(
            reviews_count=Count("reviews", distinct=True),
            average_rating=Avg("reviews__rating"),
        )
        return qs

class ProductListCreate(ProductFilterMixin, generics.ListCreateAPIView):
    serializer_class = ProductSerializer
    permission_classes = [AllowAny]
    authentication_classes = []
    filter_backends = [filters.SearchFilter]
    search_fields = ['title_en', 'title_de']
    parser_classes = [MultiPartParser, FormParser]
    def get_queryset(self):
        return self.filtered_products_qs()

class ProductListView(ProductFilterMixin, generics.ListAPIView):
    serializer_class = ProductSerializer
    permission_classes = [AllowAny]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["title_en", "title_de"]
    ordering_fields = ("created_at", "price_gross", "id", "title_en", "title_de")
    ordering = ("-created_at", "id")
    def get_queryset(self):
        return self.filtered_products_qs()

class ProductDetailView(generics.RetrieveAPIView):
    serializer_class = ProductSerializer
    permission_classes = [AllowAny]
    def get_queryset(self):
        return Product.objects.annotate(
            reviews_count=Count("reviews", distinct=True),
            average_rating=Avg("reviews__rating"),
        )

class ProductSuggestList(generics.ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = ProductMiniSerializer
    filter_backends = [filters.SearchFilter]
    pagination_class = None
    def get_queryset(self):
        term = (self.request.query_params.get("search") or "").strip()
        if not term:
            return Product.objects.none()
        return Product.objects.filter(
            Q(title_en__icontains=term) | Q(title_de__icontains=term)
        ).order_by(Lower("title_en")).all()[:10]

class ProductReviewListCreate(generics.ListCreateAPIView):
    parser_classes = [MultiPartParser, FormParser]
    def get_permissions(self):
        return [permissions.IsAuthenticated()] if self.request.method == "POST" else [permissions.AllowAny()]
    def get_queryset(self):
        return Review.objects.filter(product_id=self.kwargs["pk"]).order_by("-created_at")
    def get_serializer_class(self):
        return ReviewWriteSerializer if self.request.method == "POST" else ReviewSerializer
    def get_serializer_context(self):
        return {"request": self.request}
    def create(self, request, *args, **kwargs):
        write_ser = ReviewWriteSerializer(data=request.data)
        write_ser.is_valid(raise_exception=True)
        product = get_object_or_404(Product, pk=kwargs["pk"])
        review_user = request.user
        if request.user.is_staff:
            maybe_user = write_ser.validated_data.pop("user", None)
            if maybe_user:
                review_user = maybe_user
        instance = Review.objects.create(
            user=review_user,
            product=product,
            rating=write_ser.validated_data["rating"],
            message=write_ser.validated_data.get("message", ""),
            image=write_ser.validated_data.get("image"),
            author_name=write_ser.validated_data.get("author_name", ""),
            author_avatar=write_ser.validated_data.get("author_avatar"),
        )
        read_ser = ReviewSerializer(instance, context={"request": request})
        headers = self.get_success_headers(read_ser.data)
        return Response(read_ser.data, status=201, headers=headers)

class ReviewListAPIView(generics.ListAPIView):
    serializer_class = ReviewSerializer
    permission_classes = [permissions.IsAuthenticated]
    def get_queryset(self):
        return Review.objects.filter(user=self.request.user).order_by("-created_at")
    def get_serializer_context(self):
        return {"request": self.request}

class ReviewDetailAPIView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Review.objects.all()
    permission_classes = [permissions.IsAuthenticated, IsOwnerOrAdmin]
    parser_classes = [MultiPartParser, FormParser]
    def get_serializer_class(self):
        return ReviewWriteSerializer if self.request.method in ("PUT", "PATCH") else ReviewSerializer
    def get_serializer_context(self):
        return {"request": self.request}

# -----------------------------------------------------------------------------
# Contact / Newsletter
# -----------------------------------------------------------------------------
class ContactMessageCreateView(generics.CreateAPIView):
    queryset = ContactMessage.objects.all()
    serializer_class = ContactMessageSerializer
    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    def perform_create(self, serializer):
        instance = serializer.save()
        send_mail(
            subject=f"New Contact Us message from {instance.name}",
            message=f"Name: {instance.name}\nEmail: {instance.email}\n\nMessage:\n{instance.message}",
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", settings.EMAIL_HOST_USER),
            recipient_list=[getattr(settings, "ADMIN_EMAIL", settings.EMAIL_HOST_USER)],
            fail_silently=True,
        )

class NewsletterSubscriberCreateView(generics.CreateAPIView):
    queryset = NewsletterSubscriber.objects.all()
    serializer_class = NewsletterSubscriberSerializer
    permission_classes = [AllowAny]
    authentication_classes = []
    def create(self, request, *args, **kwargs):
        email = (request.data.get("email") or "").strip().lower()
        if not email:
            return Response({"email": ["This field is required."]}, status=400)
        obj, created = NewsletterSubscriber.objects.get_or_create(email=email)
        # admin notify
        try:
            admin_html = render_to_string("emails/admin_newsletter_notify.html", {"email": obj.email})
            admin_msg = EmailMultiAlternatives(
                subject="New Newsletter Subscription",
                body=f"New subscriber: {obj.email}",
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", settings.EMAIL_HOST_USER),
                to=[getattr(settings, "ADMIN_EMAIL", settings.EMAIL_HOST_USER)],
            )
            admin_msg.attach_alternative(admin_html, "text/html")
            admin_msg.send(fail_silently=True)
        except Exception:
            pass
        # user thank-you
        try:
            user_html = render_to_string("emails/newsletter_welcome.html", {"email": obj.email, "already": not created})
            user_msg = EmailMultiAlternatives(
                subject="Thanks for subscribing to MindMines",
                body="Thanks for subscribing to MindMines!",
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", settings.EMAIL_HOST_USER),
                to=[obj.email],
            )
            user_msg.attach_alternative(user_html, "text/html")
            user_msg.send(fail_silently=True)
        except Exception:
            pass
        data = NewsletterSubscriberSerializer(obj, context={"request": request}).data
        return Response(data, status=201 if created else 200)

class NewsletterSubscriberListView(generics.ListAPIView):
    queryset = NewsletterSubscriber.objects.all().order_by("-subscribed_at")
    serializer_class = NewsletterSubscriberSerializer
    permission_classes = [IsAdminUser]
    pagination_class = StandardResultsSetPagination

class ContactMessageListView(generics.ListAPIView):
    queryset = ContactMessage.objects.all().order_by("-sent_at")
    serializer_class = ContactMessageSerializer
    permission_classes = [IsAdminUser]
    pagination_class = StandardResultsSetPagination

# -----------------------------------------------------------------------------
# Wishlist
# -----------------------------------------------------------------------------
class WishlistView(generics.ListCreateAPIView):
    serializer_class = WishlistSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = StandardResultsSetPagination
    def get_queryset(self):
        return Wishlist.objects.filter(user=self.request.user).order_by("id")
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

class WishlistDetailView(generics.DestroyAPIView):
    serializer_class = WishlistSerializer
    permission_classes = [IsAuthenticated]
    def get_object(self):
        return Wishlist.objects.get(user=self.request.user, product_id=self.kwargs["pk"])

# -----------------------------------------------------------------------------
# Orders (read/create/invoice)
# -----------------------------------------------------------------------------
class OrderListAPIView(generics.ListAPIView):
    serializer_class = OrderReadSerializer
    permission_classes = [IsAuthenticated]
    def get_queryset(self):
        return Order.objects.filter(user=self.request.user).order_by("-created_at")
    def list(self, request, *args, **kwargs):
        ser = self.get_serializer(self.get_queryset(), many=True, context={"request": request})
        return Response(ser.data)

class OrderCreateView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        ser = OrderCreateSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)
        with transaction.atomic():
            order = ser.save()
        return Response(OrderReadSerializer(order, context={"request": request}).data, status=201)

class OrderDetailPublicAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, pk: int):
        o = get_object_or_404(Order, pk=pk)
        return Response({
            "id": o.id,
            "number": o.number,
            "status": o.status,
            "payment_status": getattr(o, "payment_status", ""),
            "total_price": f"{o.total_price:.2f}",
            "created_at": o.created_at,
        })
    
class OrderDetailAPIView(generics.RetrieveAPIView):
    serializer_class = OrderReadSerializer
    permission_classes = [IsAuthenticated]
    def get_queryset(self):
        return Order.objects.filter(user=self.request.user)
    def retrieve(self, request, *args, **kwargs):
        ser = self.get_serializer(self.get_object(), context={"request": request})
        return Response(ser.data)

class OrderInvoiceAPIView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request, pk):
        try:
            order = Order.objects.get(pk=pk, user=request.user)
        except Order.DoesNotExist:
            raise Http404("Order not found")
        if not hasattr(order, "invoice") or not order.invoice.pdf:
            generate_invoice_pdf(order)
        inv = order.invoice
        return FileResponse(inv.pdf.open("rb"),
                            content_type="application/pdf",
                            as_attachment=True,
                            filename=f"invoice_{inv.number}.pdf")

class OrderAdminUpdateAPIView(APIView):
    permission_classes = [IsAdminUser]
    def patch(self, request, pk):
        order = get_object_or_404(Order, pk=pk)
        allowed = {"status", "tracking_number", "tracking_carrier", "tracking_url", "shipped_at", "delivered_at"}
        prev_status = order.status
        updated = False
        for field in allowed:
            if field in request.data and hasattr(order, field):
                setattr(order, field, request.data[field])
                updated = True
        if not updated:
            return Response({"detail": "No updatable fields provided."}, status=400)
        order.save()

        if prev_status != "paid" and order.status == "paid":
            _safe(order.reserve_stock_atomic)

        elif prev_status != "failed" and order.status == "failed":
            _safe(order.release_stock_atomic)

        return Response(OrderReadSerializer(order, context={"request": request}).data, status=200)

class SetTrackingAPIView(APIView):
    permission_classes = [IsAdminUser]
    def post(self, request, pk):
        order = get_object_or_404(Order, pk=pk)
        order.tracking_number = (request.data.get("tracking_number") or "").strip()
        order.tracking_carrier = (request.data.get("tracking_carrier") or "").strip()
        order.tracking_url = (request.data.get("tracking_url") or "").strip()
        if not order.shipped_at:
            order.shipped_at = timezone.now()
        order.save(update_fields=["tracking_number","tracking_carrier","tracking_url","shipped_at"])
        _safe(generate_invoice_pdf, order)
        user_locale = getattr(getattr(order.user, "profile", None), "language", "en") or "en"
        _safe(send_invoice_email, order, user_locale=user_locale, to=_order_to(order))
        return Response({"status": "ok", "invoice": getattr(getattr(order, "invoice", None), "number", "")})

# -----------------------------------------------------------------------------
# Coupons / Promotions
# -----------------------------------------------------------------------------
class CouponPreviewAPIView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        s = CouponPreviewIn(data=request.data)
        s.is_valid(raise_exception=True)
        code  = s.validated_data["code"]
        items = s.validated_data["items"]

        subtotal = Decimal("0.00")
        for it in items:
            p = get_object_or_404(Product, pk=it["product_id"])
            subtotal += Decimal(p.final_price_gross) * int(it["qty"])

        from server.models import Coupon
        coup = Coupon.objects.filter(code__iexact=code.strip()).first()
        if not coup:
            return Response({"ok": False, "error": "Coupon not found"}, status=404)

        user_or_none = request.user if request.user.is_authenticated else None
        ok, reason = coup.can_apply(user_or_none, items, subtotal)
        if not ok:
            return Response({"ok": False, "error": reason}, status=400)

        discount = coup.discount_amount(user_or_none, items, subtotal)
        total = (subtotal - discount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return Response({
            "ok": True,
            "code": coup.code,
            "kind": coup.kind,
            "value": str(coup.value),
            "discount": f"{discount:.2f}",
            "subtotal": f"{subtotal:.2f}",
            "total": f"{total:.2f}",
        })

class ActivePromotionAPIView(APIView):
    permission_classes = [AllowAny]
    def get(self, request):
        now = timezone.now()
        country = (request.GET.get("country") or request.headers.get("X-Country") or "DE").upper()
        qs = PromotionBanner.objects.filter(
            is_enabled=True, start_at__lte=now, end_at__gte=now
        ).order_by("-priority", "-updated_at")
        qs = qs.filter(Q(country_whitelist=[]) | Q(country_whitelist__contains=[country]))
        promo = qs.first()
        if not promo:
            return Response({"active": False}, status=200)
        data = PromotionSerializer(promo, context={"request": request}).data
        data["active"] = True
        return Response(data, status=200)

# -----------------------------------------------------------------------------
# Misc
# -----------------------------------------------------------------------------
@api_view(["GET"])
def getRoutes(request):
    return Response(["/users/token/", "/users/register/", "/users/token/refresh/"])

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def testEndPoint(request):
    if request.method == "GET":
        data = f"Congratulation {request.user}, your API just responded to GET request"
        return Response({"response": data}, status=200)
    text = "Hello buddy"
    data = f"Congratulation your API just responded to POST request with text: {text}"
    return Response({"response": data}, status=200)

@api_view(['GET'])
def verify_email(request, uidb64, token):
    try:
        uid = urlsafe_base64_decode(uidb64).decode()
        user = User.objects.get(pk=uid)
    except Exception:
        return Response({"detail": "Invalid user ID"}, status=400)

    if user.is_active:
        return Response({"detail": "Account already verified"}, status=200)

    if default_token_generator.check_token(user, token):
        user.is_active = True
        user.save()
        try:
            subj = "Welcome to MindMines / Willkommen bei MindMines"
            en = ("Welcome! Your email has been verified successfully.<br/>"
                  "You can now sign in and start shopping. Have fun!<br/>"
                  "<a href='https://mindmines.eu'>mindmines.eu</a>")
            de = ("Willkommen! Ihre E-Mail wurde erfolgreich verifiziert.<br/>"
                  "Sie können sich jetzt anmelden und einkaufen. Viel Spaß!<br/>"
                  "<a href='https://mindmines.eu'>mindmines.eu</a>")
            _send_bilingual(subj, user.email, f"<p>{en}</p>", f"<p>{de}</p>")
        except Exception:
            logger.exception("Failed to send happy-shopping email after verification")
        return Response({"detail": "Email verified successfully"}, status=200)

    return Response({"detail": "Invalid or expired token"}, status=400)

@api_view(["POST"])
@permission_classes([AllowAny])
def resend_verification_email(request):
    email_addr = request.data.get("email")
    if not email_addr:
        return Response({"detail": "Email is required."}, status=400)
    try:
        user = User.objects.get(email=email_addr)
    except User.DoesNotExist:
        return Response({"detail": "User not found."}, status=404)
    if user.is_active:
        return Response({"detail": "Account already verified."}, status=400)
    uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    base = getattr(settings, "FRONTEND_URL", "https://mindmines.eu").rstrip("/")
    link = f"{base}/verify-email/{uidb64}/{token}/"
    subject = "Resend: Verify your MindMines account"
    text_body = f"Please verify your email:\n{link}"
    from_email = settings.EMAIL_HOST_USER
    html_body = None
    try:
        html_body = render_to_string("emails/verification_email.html", {"user": user, "verification_link": link})
    except TemplateDoesNotExist:
        pass
    msg = EmailMultiAlternatives(subject=subject, body=text_body, from_email=from_email, to=[user.email])
    if html_body:
        msg.attach_alternative(html_body, "text/html")
    msg.send(fail_silently=not settings.DEBUG)
    payload = {"detail": "Verification email sent."}
    if settings.DEBUG:
        payload["dev_verification_link"] = link
    return Response(payload, status=200)

# -----------------------------------------------------------------------------
# PayPal
# -----------------------------------------------------------------------------
def d2(x):
    return Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

class PaypalBase(APIView):
    """
    Base for PayPal.
    Supports:
    - PayPal normal card/balance
    - PayPal Pay Later (PayPal decides installments)
    """
    permission_classes = [permissions.AllowAny]

    def _creds_ok(self):
        return bool(settings.PAYPAL_CLIENT_ID and settings.PAYPAL_CLIENT_SECRET)

    def _base(self):
        # live vs sandbox
        return (
            "https://api-m.paypal.com"
            if getattr(settings, "PAYPAL_ENV", "live") == "live"
            else "https://api-m.sandbox.paypal.com"
        )

    def _token(self):
        try:
            r = http.post(
                f"{self._base()}/v1/oauth2/token",
                data={"grant_type": "client_credentials"},
                auth=(settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET),
                timeout=30,
            )
        except Exception as e:
            return None, {"error": "token_request_failed", "detail": str(e)}

        if r.status_code != 200:
            body = {}
            try:
                body = r.json()
            except Exception:
                pass
            return None, {"error": "token_http_error", "status": r.status_code, "body": body}

        data = r.json()
        return data.get("access_token"), None

class PaypalCreateOrder(PaypalBase):
    """
    1. Create local Order(status=pending, payment_method='paypal').
    2. Call PayPal /v2/checkout/orders with intent CAPTURE.
    3. Return {id: paypal_id, order_id: local_order_id} to frontend.
    """

    def post(self, request):
        if not self._creds_ok():
            return Response({"detail": "PayPal credentials not configured."}, status=400)

        ser = OrderCreateSerializer(data=request.data, context={"request": request})
        try:
            ser.is_valid(raise_exception=True)
        except serializers.ValidationError as ve:
            return Response({"detail": "Order validation failed", "errors": ve.detail}, status=400)

        # create local order
        try:
            with transaction.atomic():
                order = ser.save()
                order.payment_method = "paypal"
                order.status = "pending"
                order.save(update_fields=["payment_method", "status"])
        except Exception as e:
            return Response({"detail": "LOCAL_ORDER_CREATE_FAILED", "error": str(e)}, status=500)

        token, err = self._token()
        if not token:
            return Response({"detail": "FAILED_TO_GET_TOKEN", "error": err}, status=502)

        payload = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {"currency_code": "EUR", "value": f"{order.total_price:.2f}"},
                "reference_id": str(order.id),
            }],
            "application_context": {
                "brand_name": "MindMines",
                "landing_page": "LOGIN",
                "user_action": "PAY_NOW",
                "shipping_preference": "NO_SHIPPING",
            },
        }

        resp = http.post(
            f"{self._base()}/v2/checkout/orders",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=30,
        )

        try:
            data = resp.json()
        except Exception:
            data = {}

        if resp.status_code not in (200, 201):
            return Response({"detail": "PAYPAL_CREATE_ERROR", "paypal": data}, status=502)

        try:
            order.payment_ref = data.get("id", "")
            order.save(update_fields=["payment_ref"])
        except Exception as e:
            return Response({"detail": "LOCAL_UPDATE_FAILED", "error": str(e)}, status=500)

        return Response({"id": data.get("id"), "order_id": order.id}, status=201)

class PaypalCaptureOrder(PaypalBase):
    """
    Frontend gives us:
      - PayPal order ID (data.orderID)
      - local_order_id (what we created)
    We:
      - capture via PayPal API
      - mark local order paid if COMPLETED
    """

    def post(self, request, order_id):
        if not self._creds_ok():
            return Response({"detail": "PayPal credentials not configured."}, status=400)

        local_order_id = request.data.get("local_order_id")
        oid_query = {"payment_ref": order_id}
        if local_order_id:
            oid_query["id"] = local_order_id

        try:
            with transaction.atomic():
                order = Order.objects.select_for_update().get(**oid_query)
        except Order.DoesNotExist:
            return Response({"detail": "Local order not found for this PayPal order."}, status=404)

        if order.status == "paid":
            return Response({"status": "COMPLETED"}, status=200)

        token, err = self._token()
        if not token:
            return Response({"detail": "FAILED_TO_GET_TOKEN", "error": err}, status=502)

        cap_url = f"{self._base()}/v2/checkout/orders/{order_id}/capture"

        resp = http.post(
            cap_url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=30,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}

        if resp.status_code not in (200, 201):
            return Response({"detail": "PAYPAL_CAPTURE_ERROR", "paypal": data}, status=502)

        if data.get("status") != "COMPLETED":
            return Response({"detail": "CAPTURE_NOT_COMPLETED", "paypal": data}, status=400)

        # we consider "COMPLETED" as paid.
        with transaction.atomic():
            order.status = "paid"
            order.payment_status = "paid"
            order.paid_at = timezone.now()
            order.save(update_fields=["status","payment_status","paid_at"])

            # stock reservation
            _safe(order.reserve_stock_atomic)

        return Response({"status": "COMPLETED"}, status=200)

# -----------------------------------------------------------------------------
# Stripe — one-off PI + setups + installments
# -----------------------------------------------------------------------------
PAYMENT_METHOD_MAP = {"card": "card", "sepa_debit": "sepa_debit", "giropay": "giropay", "klarna": "klarna"}

INTENT_METHOD_ALIASES = {
    "stripe_card": "card",
    "stripe_klarna": "klarna",
    "stripe_giropay": "giropay",
    "debit_card": "card",
}

INTENT_METHOD_KEYS = (
    "payment_method",
    "paymentMethod",
    "method",
    "payment_method_type",
    "type",
)

KLARNA_LOCALE_MAP = {
    "AT": "de-AT",
    "DE": "de-DE",
    "DK": "da-DK",
    "FI": "fi-FI",
    "NL": "nl-NL",
    "NO": "nb-NO",
    "SE": "sv-SE",
}


def _resolve_intent_method(order, data):
    """Return (normalized_method, should_update_order)."""
    supported = set(PAYMENT_METHOD_MAP.keys()) | {"card", "giropay", "klarna"}
    raw_value = None
    method_source = "default"

    for key in INTENT_METHOD_KEYS:
        if key in data:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                raw_value = value
                method_source = "request"
                break

    if raw_value is None:
        existing = getattr(order, "payment_method", "") or ""
        if existing:
            raw_value = existing
            method_source = "order"

    normalized = (raw_value or "card").strip().lower()
    normalized = INTENT_METHOD_ALIASES.get(normalized, normalized)
    if normalized not in supported:
        normalized = "card"
        if method_source == "order":
            method_source = "default"

    should_update = False
    if method_source == "request":
        should_update = True
    elif method_source == "order":
        should_update = ((order.payment_method or "").strip().lower() != normalized)
    elif method_source == "default" and not order.payment_method:
        should_update = True

    return normalized, should_update


def _klarna_locale(order, data):
    locale = None
    for key in ("klarna_locale", "locale", "preferred_locale"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            locale = value.strip()
            break

    if not locale:
        country = data.get("klarna_country") or getattr(order, "ship_country", "") or ""
        country = str(country).upper()
        locale = KLARNA_LOCALE_MAP.get(country)

    return locale or "en-GB"


def _giropay_return_url(data):
    for key in ("return_url", "giropay_return_url"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    configured = getattr(settings, "STRIPE_GIROPAY_RETURN_URL", "").strip()
    if configured:
        return configured

    base = getattr(settings, "FRONTEND_URL", "").rstrip("/")
    return base or None


def _payment_intent_method_kwargs(method, order, data):
    kwargs = {
        "automatic_payment_methods": {"enabled": False},
        "payment_method_types": [PAYMENT_METHOD_MAP.get(method, method or "card")],
    }

    options = {}
    if method == "klarna":
        options.setdefault("klarna", {})["preferred_locale"] = _klarna_locale(order, data)
    elif method == "giropay":
        return_url = _giropay_return_url(data)
        if return_url:
            options.setdefault("giropay", {})["return_url"] = return_url

    if options:
        kwargs["payment_method_options"] = options

    return kwargs

def _amount_cents(value: Decimal) -> int:
    return int((value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

def _split_installments_eur(total_cents: int, months: int) -> List[Decimal]:
    base = total_cents // months
    remainder = total_cents - base * months
    cents_array = [base] * months
    if remainder:
        cents_array[-1] += remainder
    return [Decimal(c) / Decimal(100) for c in cents_array]

def _month_dates(anchor_dt, months: int):
    return [anchor_dt + relativedelta(months=i) for i in range(months)]

def _normalize_payment_intent(obj):
    """
    Accepts:
      - (intent, client_secret) tuple
      - dict with 'id'/'client_secret'
      - stripe.PaymentIntent object with attributes
    Returns: (intent_id, client_secret)
    """
    # tuple form: (payment_intent, client_secret)
    if isinstance(obj, tuple) and len(obj) >= 2:
        pi, cs = obj[0], obj[1]
        pi_id = getattr(pi, "id", None)
        if pi_id is None and isinstance(pi, dict):
            pi_id = pi.get("id")
        return pi_id, cs

    # dict form
    if isinstance(obj, dict):
        return obj.get("id"), obj.get("client_secret")

    # object form
    return getattr(obj, "id", None), getattr(obj, "client_secret", None)

def _get_or_create_stripe_customer(user=None, email: Optional[str] = None) -> Optional[str]:
    """
    Create/reuse Stripe customer for authenticated checkout
    (helps with installments/off-session).
    Guests can just be None for one-shot PI.
    """
    if user and getattr(user, "is_authenticated", False):
        prof = getattr(user, "profile", None)
        if prof and getattr(prof, "stripe_customer_id", None):
            return prof.stripe_customer_id
        customer = stripe.Customer.create(
            email=(getattr(user, "email", None) or email) or None,
            name=(f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip() or None),
            metadata={"user_id": str(user.id)},
        )
        if prof and hasattr(prof, "stripe_customer_id"):
            prof.stripe_customer_id = customer.id
            prof.save(update_fields=["stripe_customer_id"])
        return customer.id
    return None

def stripe_client():
    stripe.api_key = settings.STRIPE_SECRET_KEY
    stripe.set_app_info(name="MindMines Payment", version="1.0.0")
    return stripe

def eur_cents(amount: Decimal) -> int:
    # Quantize to 2dp, convert to Stripe's integer amount
    q = Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(q * 100)

# ---------------------------
# STRIPE: immediate one-shot (card / klarna / giropay / sofort etc.)
# ---------------------------
class StripeCreateIntentAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        try:
            data = request.data
            order_id = int(data.get("order_id") or 0)
            if not order_id:
                return Response({"detail": "order_id is required"}, status=400)

            # no select_for_update here
            order = Order.objects.get(pk=order_id)
        except Order.DoesNotExist:
            return Response({"detail": "Order not found"}, status=404)
        except Exception as e:
            return Response({"detail": f"Invalid payload: {e}"}, status=400)

        if order.total_price <= Decimal("0.00"):
            return Response(
                {"detail": "Your total is 0.00 € after discounts. We can't create an online payment for 0 €."},
                status=400,
            )

        # mark processing
        method, update_payment_method = _resolve_intent_method(order, data)

        update_fields = ["payment_status"]
        order.payment_status = "processing"
        if update_payment_method:
            order.payment_method = method
            update_fields.append("payment_method")
        order.save(update_fields=update_fields)


        # Reserve stock safely (this has its own transaction.atomic() and row locks)
        try:
            if not order.stock_locked:
                order.lock_stock()
        except Exception as e:
            return Response({"detail": f"Not enough stock: {e}"}, status=400)

        stripe = stripe_client()
        amount_cents = eur_cents(order.total_price)
        description = f"MindMines Payment Order {order.number}"
        meta = {"order_id": str(order.pk), "order_number": order.number, "email": order.email}

        method_kwargs = _payment_intent_method_kwargs(method, order, data)

        if order.stripe_pi:
            try:
                pi = stripe.PaymentIntent.modify(
                    order.stripe_pi,
                    amount=amount_cents,
                    currency=settings.STRIPE_CURRENCY,
                    description=description,
                    metadata=meta,
                    **method_kwargs,
                )
            except stripe.error.InvalidRequestError:
                pi = stripe.PaymentIntent.create(
                    amount=amount_cents,
                    currency=settings.STRIPE_CURRENCY,
                    description=description,
                    metadata=meta,
                    capture_method="automatic",
                    **method_kwargs,
                )
                order.stripe_pi = pi.id
                order.payment_ref = ""
                order.payment_id = ""
                order.save(update_fields=["stripe_pi", "payment_ref", "payment_id"])
        else:
            pi = stripe.PaymentIntent.create(
                amount=amount_cents,
                currency=settings.STRIPE_CURRENCY,
                description=description,
                metadata=meta,
                capture_method="automatic",
                **method_kwargs,
            )
            order.stripe_pi = pi.id
            order.save(update_fields=["stripe_pi"])

        return Response(
            {
                "order_id": order.pk,
                "order_number": order.number,
                "amount": str(order.total_price),
                "currency": settings.STRIPE_CURRENCY,
                "payment_intent_id": pi.id,
                "client_secret": pi.client_secret,
            },
            status=201,
        )

@method_decorator(csrf_exempt, name="dispatch")
class StripeWebhookView(APIView):
    """
    Handles Stripe webhook events:
      - payment_intent.succeeded -> mark order paid, set paid_at, generate invoice, send emails
      - payment_intent.payment_failed -> mark order failed, release stock
      - charge.refunded / payment_intent.canceled -> set status accordingly, release stock if needed
    """
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        payload = request.body
        sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")
        stripe = stripe_client()
        endpoint_secret = settings.STRIPE_WEBHOOK_SECRET

        try:
            event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
        except ValueError:
            return HttpResponseBadRequest("Invalid payload")
        except stripe.error.SignatureVerificationError:
            return HttpResponse(status=400)

        # Route by event type
        event_type = event.get("type")
        data = event.get("data", {}).get("object", {})

        if event_type == "payment_intent.succeeded":
            pi_id = data.get("id")
            metadata = data.get("metadata", {}) or {}
            order_id = metadata.get("order_id")

            order = None
            if order_id:
                try:
                    order = Order.objects.get(pk=int(order_id))
                except Exception:
                    order = None
            if not order and pi_id:
                order = Order.objects.filter(stripe_pi=pi_id).first()

            if order:
                latest_charge = data.get("latest_charge")
                order.payment_status = "paid"
                order.status = "paid"
                order.paid_at = timezone.now()
                order.payment_ref = pi_id or order.payment_ref
                order.payment_id = latest_charge or order.payment_id
                order.save(update_fields=[
                    "payment_status", "status", "paid_at",
                    "payment_ref", "payment_id"
                ])

            return HttpResponse(status=200)

        elif event_type == "payment_intent.payment_failed":
            pi_id = data.get("id")
            order = Order.objects.filter(stripe_pi=pi_id).first()
            if order:
                order.payment_status = "failed"
                order.status = "failed"
                order.save(update_fields=["payment_status", "status"])
                # Release stock if it was held
                try:
                    if order.stock_locked:
                        order.release_stock()
                except Exception:
                    pass
            return HttpResponse(status=200)

        elif event_type in ("payment_intent.canceled", "charge.refunded", "charge.refund.updated"):
            # Conservative: move to processing/cancelled accordingly and free stock
            pi_id = data.get("payment_intent") or data.get("id")
            order = Order.objects.filter(stripe_pi=pi_id).first()
            if order:
                order.status = "cancelled" if event_type == "payment_intent.canceled" else order.status
                order.save(update_fields=["status"])
                try:
                    if order.stock_locked and not order.stock_released:
                        order.release_stock()
                except Exception:
                    pass
            return HttpResponse(status=200)

        # Unhandled:
        return HttpResponse(status=200)  

class StripeCreateIntent(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        ser = OrderCreateSerializer(data=request.data, context={"request": request})
        try:
            ser.is_valid(raise_exception=True)
        except serializers.ValidationError as ve:
            return Response({"detail": "Order validation failed", "errors": ve.detail}, status=400)

        try:
            with transaction.atomic():
                order = ser.save()
                order.payment_method = "card"
                order.status = "pending"
                order.save(update_fields=["payment_method", "status"])
        except Exception as e:
            return Response({"detail": f"Order creation failed: {e}"}, status=400)

        total = Decimal(order.total_price or "0.00")
        if total <= Decimal("0.00"):
            order.status = "failed"
            order.save(update_fields=["status"])
            return Response({"detail": "Total must be > 0."}, status=400)

        try:
            intent = stripe.PaymentIntent.create(
                amount=_amount_cents(total),
                currency=getattr(settings, "STRIPE_CURRENCY", "eur"),
                payment_method_types=["card"],
                description=f"Order {order.number or order.id} — MindMines Payment",
                metadata={"order_id": str(order.id)},
                receipt_email=order.email or None,
            )
        except stripe_error.StripeError as e:
            order.status = "failed"
            order.save(update_fields=["status"])
            return Response({"detail": f"Stripe error: {str(e)}"}, status=502)

        order.payment_ref = intent.id
        order.stripe_pi = intent.id
        order.save(update_fields=["payment_ref", "stripe_pi"])

        return Response({"clientSecret": intent.client_secret, "order_id": order.id}, status=201)

# ---------------------------
# STRIPE: card installments (authenticated only)
# ---------------------------
class StripeCardInstallmentsView(APIView):
    """
    User picked installments with their saved card.
    Flow:
    1. Frontend saves card via SetupIntent -> gets payment_method_id
    2. Calls this endpoint with {months, payment_method_id, ...same checkout payload...}
    3. We create Order(status=processing), Stripe SubscriptionSchedule with monthly prices.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        months = int(request.data.get("months") or 0)
        if months not in (3, 6, 9):
            return Response({"detail": "Invalid months. Choose 3, 6, or 9."}, status=400)

        ser = OrderCreateSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)

        # build order
        try:
            with transaction.atomic():
                order = ser.save()
                order.payment_method = "card_installments"
                order.installments_months = months
                order.status = "processing"
                order.save(update_fields=["payment_method", "installments_months", "status"])
        except Exception as e:
            return Response({"detail": f"Order creation failed: {e}"}, status=500)

        payment_method_id = request.data.get("payment_method_id")
        if not payment_method_id:
            return Response({"detail": "payment_method_id is required"}, status=400)

        # get/create stripe customer, attach card, schedule charges
        try:
            pm = stripe.PaymentMethod.retrieve(payment_method_id)
            pm_customer = pm.get("customer")
            if pm_customer:
                customer_id = pm_customer
            else:
                customer_id = _get_or_create_stripe_customer(request.user, email=order.email)
                stripe.PaymentMethod.attach(payment_method_id, customer=customer_id)

            stripe.Customer.modify(
                customer_id,
                invoice_settings={"default_payment_method": payment_method_id},
            )

            total_cents = _amount_cents(order.total_price)
            base = total_cents // months
            remainder = total_cents - base * months

            # helper to make a recurring price
            def mk_price(amount_int, label):
                return stripe.Price.create(
                    unit_amount=amount_int,
                    currency=getattr(settings, "STRIPE_CURRENCY", "eur"),
                    recurring={"interval": "month"},
                    product_data={"name": label},
                )

            now_ = timezone.now()

            def ts(dt):
                return int(dt.timestamp())

            title_base = f"Installments for Order {order.number or order.id}"
            phases = []
            if remainder:
                if months > 1:
                    price_base = mk_price(base, f"{title_base} (monthly)")
                    phases.append({
                        "items": [{"price": price_base.id, "quantity": 1}],
                        "end_date": ts(now_ + timedelta(days=30*(months-1))),
                    })
                price_last = mk_price(base + remainder, f"{title_base} (final)")
                phases.append({
                    "items": [{"price": price_last.id, "quantity": 1}],
                    "end_date": ts(now_ + timedelta(days=30*months)),
                })
            else:
                price_all = mk_price(base, f"{title_base} (monthly)")
                phases.append({
                    "items": [{"price": price_all.id, "quantity": 1}],
                    "end_date": ts(now_ + timedelta(days=30*months)),
                })

            schedule = stripe.SubscriptionSchedule.create(
                customer=customer_id,
                start_date="now",
                end_behavior="cancel",
                phases=phases,
                default_settings={
                    "default_payment_method": payment_method_id,
                    "collection_method": "charge_automatically",
                },
                metadata={"order_id": str(order.id)},
            )

            order.payment_ref = schedule.id
            order.save(update_fields=["payment_ref"])
            
            return Response({"order_id": order.id, "schedule_id": schedule.id}, status=201)

        except stripe_error.StripeError as e:
            return Response({"detail": f"Stripe error: {str(e)}"}, status=502)
        except Exception as e:
            return Response({"detail": f"Schedule create failed: {e}"}, status=500)

# ---------------------------
# STRIPE: SEPA one-time (DE only)
# ---------------------------
class StripeCreateSepaIntent(APIView):
    """
    Create local order, PaymentIntent with payment_method_types=["sepa_debit"].
    Frontend then calls stripe.confirmSepaDebitPayment().
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        ser = OrderCreateSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)

        with transaction.atomic():
            order = ser.save()
            order.payment_method = "sepa_debit"
            order.status = "pending"
            order.save(update_fields=["payment_method", "status"])

        # we *require* email for mandate
        email = (order.email or "").strip()
        if not email:
            return Response({"detail": "Email is required for SEPA payments."}, status=400)

        try:
            customer_id = _get_or_create_stripe_customer(
                user=request.user if getattr(request, "user", None) and request.user.is_authenticated else None,
                email=email,
            )
            intent = stripe.PaymentIntent.create(
                amount=_amount_cents(order.total_price),
                currency=getattr(settings, "STRIPE_CURRENCY", "eur"),
                payment_method_types=["sepa_debit"],
                description=f"Order {order.number or order.id} — MindMines Payment",
                metadata={"order_id": str(order.id)},
                customer=customer_id or None,
                receipt_email=email,
                confirm=False,
            )
        except stripe_error.StripeError as e:
            order.status = "failed"
            order.save(update_fields=["status"])
            return Response({"detail": str(e)}, status=502)

        order.payment_ref = intent.id
        order.stripe_pi = intent.id
        order.save(update_fields=["payment_ref", "stripe_pi"])

        return Response(
            {"clientSecret": intent.client_secret, "order_id": order.id},
            status=201,
        )

# ---------------------------
# STRIPE: SEPA installments (DE only)
# ---------------------------
class StripeSepaInstallmentsView(APIView):
    """
    Same idea as card installments, but for SEPA mandate.
    Germany only. User must be logged in (we need off-session charges).
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        months = int(request.data.get("months") or 0)
        country = (request.data.get("country") or "").upper()[:2]

        if months not in (3, 6, 9):
            return Response({"detail": "Invalid months. Choose 3, 6, or 9."}, status=400)
        if country != "DE":
            return Response({"detail": "Installments are available in DE only."}, status=400)

        ser = OrderCreateSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)

        with transaction.atomic():
            order = ser.save()
            order.payment_method = "sepa_installments"
            order.installments_months = months
            order.status = "processing"
            order.save(update_fields=["payment_method", "installments_months", "status"])

        # We assume frontend already did a SetupIntent and passes payment_method_id (sepa_debit)
        payment_method_id = request.data.get("payment_method_id")
        if not payment_method_id:
            return Response({"detail": "payment_method_id is required"}, status=400)

        try:
            pm = stripe.PaymentMethod.retrieve(payment_method_id)
            pm_customer = pm.get("customer")

            if pm_customer:
                customer_id = pm_customer
            else:
                customer_id = _get_or_create_stripe_customer(request.user, email=order.email)
                stripe.PaymentMethod.attach(payment_method_id, customer=customer_id)

            stripe.Customer.modify(
                customer_id,
                invoice_settings={"default_payment_method": payment_method_id},
            )

            total_cents = _amount_cents(order.total_price)
            base = total_cents // months
            remainder = total_cents - base * months

            def mk_price(amount_int, label):
                return stripe.Price.create(
                    unit_amount=amount_int,
                    currency=getattr(settings, "STRIPE_CURRENCY", "eur"),
                    recurring={"interval": "month"},
                    product_data={"name": label},
                )

            now_ = timezone.now()
            def ts(dt): return int(dt.timestamp())

            title_base = f"SEPA installments for Order {order.number or order.id}"
            phases = []
            if remainder:
                if months > 1:
                    base_price = mk_price(base, f"{title_base} (monthly)")
                    phases.append({
                        "items": [{"price": base_price.id, "quantity": 1}],
                        "end_date": ts(now_ + timedelta(days=30*(months-1))),
                    })
                last_price = mk_price(base + remainder, f"{title_base} (final)")
                phases.append({
                    "items": [{"price": last_price.id, "quantity": 1}],
                    "end_date": ts(now_ + timedelta(days=30*months)),
                })
            else:
                all_price = mk_price(base, f"{title_base} (monthly)")
                phases.append({
                    "items": [{"price": all_price.id, "quantity": 1}],
                    "end_date": ts(now_ + timedelta(days=30*months)),
                })

            schedule = stripe.SubscriptionSchedule.create(
                customer=customer_id,
                start_date="now",
                end_behavior="cancel",
                phases=phases,
                default_settings={
                    "default_payment_method": payment_method_id,
                    "collection_method": "charge_automatically",
                },
                metadata={"order_id": str(order.id)},
            )

            order.payment_ref = schedule.id
            order.save(update_fields=["payment_ref"])

            return Response({"order_id": order.id, "schedule_id": schedule.id}, status=201)

        except stripe_error.StripeError as e:
            return Response({"detail": str(e)}, status=502)
        except Exception as e:
            return Response({"detail": str(e)}, status=500)

# ---------------------------
# SEPA post-confirm (after confirmSepaDebitPayment on frontend)
# ---------------------------
class SepaPostConfirmView(APIView):
    """
    Frontend calls this when Stripe said status == processing/succeeded.
    We mark order "processing", reserve stock, send SEPA pending email now.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        order_id = request.data.get("order_id")
        pi_status = (request.data.get("payment_intent_status") or "").lower()

        if not order_id or not pi_status:
            return Response({"detail": "Missing order_id or payment_intent_status"}, status=400)

        if pi_status not in ["processing", "succeeded", "requires_capture"]:
            return Response({"ok": False, "skipped": True}, status=200)

        order = get_object_or_404(Order, pk=order_id)

        # reserve stock immediately
        _safe(order.reserve_stock_atomic)

        order.status = "processing"
        order.payment_status = "processing"
        order.save(update_fields=["status", "payment_status"])
        
        
        return Response({"ok": True}, status=200)

# -----------------------------------------------------------------------------
# Stripe webhook
# -----------------------------------------------------------------------------
@csrf_exempt
def stripe_webhook(request):
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
    wh_secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", "")

    if not sig_header:
        return JsonResponse({"detail": "Missing Stripe-Signature header"}, status=400)

    payload = request.body
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, wh_secret)
    except stripe_error.SignatureVerificationError:
        return JsonResponse({"detail": "Invalid webhook signature"}, status=400)
    except Exception as e:
        return JsonResponse({"detail": f"Bad payload: {str(e)}"}, status=400)

    etype = event.get("type")
    data = event["data"]["object"]

    def _mark_paid(order: Order, pi_data: dict):
        changed = False
        if order.status != "paid":
            order.status = "paid"
            order.payment_status = "paid"
            order.paid_at = timezone.now()
            changed = True

        # final charge id if available
        if not order.payment_id:
            ch_id = None
            charges_block = pi_data.get("charges", {})
            ch_list = charges_block.get("data") if isinstance(charges_block, dict) else None
            if ch_list and len(ch_list) > 0:
                ch_id = ch_list[0].get("id")
            if not ch_id:
                ch_id = pi_data.get("id", "")
            if ch_id:
                order.payment_id = ch_id
                changed = True


        if changed:
            order.save(update_fields=["status","payment_status","paid_at","payment_id"])

        # reserve inventory for fulfilled orders
        _safe(order.reserve_stock_atomic)        

    # payment_intent.succeeded => normal card, klarna, sofort, giropay
    if etype == "payment_intent.succeeded":
        oid = (data.get("metadata") or {}).get("order_id")
        if oid:
            try:
                with transaction.atomic():
                    order = Order.objects.select_for_update().get(pk=oid)
                    _mark_paid(order, data)  # <— use the guarded helper
            except Order.DoesNotExist:
                pass

    elif etype == "invoice.payment_succeeded":
        oid = (data.get("metadata") or {}).get("order_id")
        if oid:
            try:
                with transaction.atomic():
                    order = Order.objects.select_for_update().get(pk=oid)
                    _mark_paid(order, data)
            except Order.DoesNotExist:
                pass

    # payment_intent.processing => SEPA direct debit / waiting bank
    elif etype == "payment_intent.processing":
        oid = (data.get("metadata") or {}).get("order_id")
        if oid:
            try:
                with transaction.atomic():
                    order = Order.objects.select_for_update().get(pk=oid)
                    if order.status != "processing":
                        order.status = "processing"
                        order.payment_status = "processing"
                        order.save(update_fields=["status", "payment_status"])
            except Order.DoesNotExist:
                pass

    elif etype in ("payment_intent.payment_failed", "invoice.payment_failed"):
        oid = (data.get("metadata") or {}).get("order_id")
        if oid:
            try:
                with transaction.atomic():
                    order = Order.objects.select_for_update().get(pk=oid)
                    order.status = "failed"
                    order.payment_status = "failed"
                    order.save(update_fields=["status", "payment_status"])
                    _safe(order.release_stock_atomic)
            except Order.DoesNotExist:
                pass

    return HttpResponse(status=200)

# -----------------------------------------------------------------------------
# Invoice download (user)
# -----------------------------------------------------------------------------
class InvoiceDownloadView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request, pk):
        order = get_object_or_404(Order, pk=pk, user=request.user)
        if not hasattr(order, "invoice") or not order.invoice.pdf:
            return Response({"detail": "Invoice not available."}, status=404)
        return FileResponse(order.invoice.pdf.open("rb"), as_attachment=True, filename=f"{order.invoice.number}.pdf")

# -----------------------------------------------------------------------------
# Returns / Cancellations
# -----------------------------------------------------------------------------
def _can_return(order: Order) -> tuple[bool, str]:
    if order.status != "delivered":
        return False, "Returns are only possible after delivery."
    if not order.delivered_at:
        return False, "Delivery timestamp missing for this order."
    now_ = timezone.now()
    age_days = (now_ - order.delivered_at).days
    if age_days > RETURNS_WINDOW_DAYS:
        return False, f"Return window ({RETURNS_WINDOW_DAYS} days) has expired."
    return True, None

class OrderRequestReturnAPIView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    @transaction.atomic
    def post(self, request, pk: int, *args, **kwargs):
        order = get_object_or_404(Order.objects.select_related("user"), pk=pk, user=request.user)
        allowed, reason = _can_return(order)
        if not allowed:
            return Response({"detail": reason}, status=status.HTTP_403_FORBIDDEN)
        message = (request.data.get("message") or "").strip()
        rr = ReturnRequest.objects.create(order=order, user=request.user, request_type="return", message=message)
        for f in request.FILES.getlist("files"):
            ReturnAttachment.objects.create(request=rr, file=f)
        email_err = None
        try:
            send_return_request_emails(rr)
            email_sent = True
        except Exception as e:
            email_sent = False
            email_err = f"{e.__class__.__name__}: {e}"
        return Response({"ok": True, "request_id": rr.id, "email_sent": email_sent, "error": email_err}, status=201)

class OrderCancelRequestAPIView(APIView):
    permission_classes = [IsAuthenticated]
    def post(self, request, pk: int):
        order = get_object_or_404(Order, pk=pk, user=request.user)
        allowed = False
        if order.status in ("pending", "processing", "shipped"):
            allowed = True
        elif order.status == "delivered" and order.delivered_at:
            CANCELLATION_WINDOW_DAYS = getattr(settings, "CANCELLATION_WINDOW_DAYS", RETURNS_WINDOW_DAYS)
            delta = timezone.now() - order.delivered_at
            allowed = (delta.days <= CANCELLATION_WINDOW_DAYS)
        if not allowed:
            return Response({"detail": "This order can no longer be cancelled."}, status=400)
        msg = (request.data.get("message") or "").strip()
        rr = ReturnRequest.objects.create(order=order, user=request.user, request_type="cancel", message=msg)
        for f in request.FILES.getlist("files"):
            ReturnAttachment.objects.create(request=rr, file=f)
        try:
            send_cancel_request_emails(rr)
        except Exception:
            logger.exception("Failed to send cancel emails for rr=%s", rr.pk)
        return Response({"id": rr.pk, "status": rr.status, "request_type": rr.request_type}, status=201)

# -----------------------------------------------------------------------------
# On-demand & Stock Alerts
# -----------------------------------------------------------------------------
def send_on_demand_request_email(req: OnDemandOrderRequest):
    product = req.product
    admin_mail = None
    if getattr(settings, "ORDER_ALERT_EMAIL", None):
        admin_mail = settings.ORDER_ALERT_EMAIL
    elif getattr(settings, "DEFAULT_FROM_EMAIL", None):
        admin_mail = settings.DEFAULT_FROM_EMAIL
    elif getattr(settings, "ADMINS", None):
        admin_mail = settings.ADMINS[0][1] if settings.ADMINS else None
    admin_mail = admin_mail or DEFAULT_FROM

    subject_en = f"On-demand request: {getattr(product, 'title_en', str(product))} x{req.quantity}"
    subject_de = f"Anfrage (auf Bestellung): {getattr(product, 'title_de', getattr(product, 'title_en', str(product)))} x{req.quantity}"
    subject = f"{subject_en} / {subject_de}"

    ctx = {
        "req": req,
        "product": product,
        "product_url": f"{getattr(settings, 'SITE_URL', 'https://mindmines.eu')}/products/{product.id}/",
        "current_stock": getattr(product, "quantity", 0),
    }

    html = None
    try:
        html = render_to_string("emails/on_demand_request.html", ctx)
    except TemplateDoesNotExist:
        pass

    text = (
        "EN:\n"
        f"Product: {getattr(product, 'title_en', str(product))}\n"
        f"Quantity: {req.quantity}\n"
        f"Customer: {req.name or '-'} <{req.email}>\n"
        f"Message: {req.message or '-'}\n"
        f"Stock now: {ctx['current_stock']}\n"
        f"Product URL: {ctx['product_url']}\n\n"
        "DE:\n"
        f"Produkt: {getattr(product, 'title_de', getattr(product, 'title_en', str(product)))}\n"
        f"Menge: {req.quantity}\n"
        f"Kunde: {req.name or '-'} <{req.email}>\n"
        f"Nachricht: {req.message or '-'}\n"
        f"Lagerbestand: {ctx['current_stock']}\n"
        f"Produktlink: {ctx['product_url']}\n"
    )

    msg = EmailMultiAlternatives(subject=subject, body=text, from_email=DEFAULT_FROM, to=[admin_mail], reply_to=[req.email])
    if html:
        msg.attach_alternative(html, "text/html")
    msg.send(fail_silently=not settings.DEBUG)

class OnDemandOrderCreateView(generics.CreateAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = OnDemandOrderRequestSerializer
    def perform_create(self, serializer):
        user = getattr(self.request, "user", None)
        instance = serializer.save(user=user if getattr(user, "is_authenticated", False) else None)
        try:
            send_on_demand_request_email(instance)
        except Exception:
            pass
        self._payload = {"ok": True, "id": instance.id}
    def create(self, request, *args, **kwargs):
        super().create(request, *args, **kwargs)
        return Response(getattr(self, "_payload", {"ok": True}), status=201)

class StockAlertCreateView(generics.CreateAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = StockAlertCreateSerializer
    def perform_create(self, serializer):
        alert = serializer.save()
        product = alert.product
        if (getattr(product, "quantity", 0) or 0) > 0:
            unsub = self._unsubscribe_url(alert)
            try:
                send_stock_alert_email(product, alert.email, unsub)
            finally:
                alert.mark_notified()
            self._payload = {"ok": True, "sent_now": True}
        else:
            self._payload = {"ok": True, "sent_now": False, "id": alert.id}
    def create(self, request, *args, **kwargs):
        super().create(request, *args, **kwargs)
        return Response(getattr(self, "_payload", {"ok": True}), status=201)
    def _unsubscribe_url(self, alert: StockAlert) -> str:
        base = getattr(settings, "FRONTEND_URL", "").rstrip("/")
        api_base = getattr(settings, "API_BASE_PATH", "/api").rstrip("/")
        return f"{base}{api_base}/stock-alerts/unsubscribe/{alert.unsubscribe_token}/"

class StockAlertUnsubscribeView(generics.DestroyAPIView):
    permission_classes = [permissions.AllowAny]
    lookup_field = "unsubscribe_token"
    queryset = StockAlert.objects.all()
    def get(self, request, *args, **kwargs):
        alert = get_object_or_404(self.get_queryset(), unsubscribe_token=kwargs.get("unsubscribe_token"))
        if alert.is_active:
            alert.is_active = False
            alert.save(update_fields=["is_active"])
        return Response({"ok": True, "unsubscribed": True}, status=200)
    def delete(self, request, *args, **kwargs):
        alert = get_object_or_404(self.get_queryset(), unsubscribe_token=kwargs.get("unsubscribe_token"))
        if alert.is_active:
            alert.is_active = False
            alert.save(update_fields=["is_active"])
        return Response(status=204)

# -----------------------------------------------------------------------------
# Birthday coupon
# -----------------------------------------------------------------------------
class SendBirthdayCouponAPIView(APIView):
    permission_classes = [IsAdminUser]
    def post(self, request):
        in_date = request.data.get("date")
        dry_run = str(request.data.get("dry_run", "false")).lower() in ("1", "true", "yes")
        percent = int(request.data.get("percent") or getattr(settings, "BIRTHDAY_COUPON_PERCENT", 10))
        valid_days = int(request.data.get("valid_days") or getattr(settings, "BIRTHDAY_COUPON_VALID_DAYS", 14))

        if in_date:
            d = parse_date(in_date)
            if not d:
                return Response({"detail": "Invalid date format. Use YYYY-MM-DD."}, status=400)
            run_dt = make_aware(datetime(d.year, d.month, d.day))
        else:
            run_dt = timezone.now()

        mmdd = run_dt.strftime("%m%d")
        starts_at = run_dt
        ends_at = run_dt + timedelta(days=valid_days)

        candidates = []
        qs = User.objects.select_related("profile").filter(is_active=True)
        for u in qs:
            email = (getattr(u, "email", "") or "").strip()
            prof = getattr(u, "profile", None)
            if not (email and prof):
                continue
            bday = getattr(prof, "date_of_birth", None)
            try:
                if bday and getattr(bday, "month", None) and getattr(bday, "day", None):
                    if bday.month == run_dt.month and bday.day == run_dt.day:
                        candidates.append(u)
            except Exception:
                continue

        if not candidates:
            return Response({"ok": True, "count": 0, "recipients": [], "dry_run": dry_run})

        if dry_run:
            recipients = [{"id": u.id, "email": u.email} for u in candidates]
            return Response({"ok": True, "dry_run": True, "count": len(recipients),
                             "recipients": recipients,
                             "defaults": {"percent": percent, "valid_days": valid_days,
                                          "window": [starts_at.isoformat(), ends_at.isoformat()]}})

        from server.models import Coupon
        created = 0
        reused = 0
        emailed = 0
        errors = []

        def ensure_coupon_for_user(user):
            code = f"BD{mmdd}-{user.id}"
            coup = Coupon.objects.filter(code=code).first()
            if coup:
                return coup, False
            coup = Coupon.objects.create(
                code=code, kind="percent", value=percent, active=True,
                starts_at=starts_at, ends_at=ends_at, is_birthday=True,
            )
            return coup, True

        for u in candidates:
            try:
                coup, was_created = ensure_coupon_for_user(u)
                created += 1 if was_created else 0
                reused += 0 if was_created else 1
                to = (u.email or "").strip()
                if not to:
                    continue
                subj = f"Happy Birthday! {percent}% off / Alles Gute! {percent}% Rabatt"
                try:
                    html = render_to_string("emails/birthday_coupon.html", {
                        "user": u, "coupon_code": coup.code, "percent": percent,
                        "valid_until": ends_at.date().isoformat(),
                        "shop_url": getattr(settings, "FRONTEND_URL", "https://mindmines.eu"),
                    })
                    text = render_to_string("emails/birthday_coupon.txt", {
                        "user": u, "coupon_code": coup.code, "percent": percent,
                        "valid_until": ends_at.date().isoformat(),
                        "shop_url": getattr(settings, "FRONTEND_URL", "https://mindmines.eu"),
                    })
                    msg = EmailMultiAlternatives(subj, text, from_email=DEFAULT_FROM, to=[to])
                    msg.attach_alternative(html, "text/html")
                    msg.send(fail_silently=not settings.DEBUG)
                    emailed += 1
                except TemplateDoesNotExist:
                    en = (f"<p>🎉 Happy Birthday! Use code <b>{coup.code}</b> for <b>{percent}% off</b>.</p>"
                          f"<p>Valid until <b>{ends_at.date().isoformat()}</b>. Enjoy shopping!</p>")
                    de = (f"<p>🎉 Alles Gute zum Geburtstag! Verwenden Sie den Code <b>{coup.code}</b> "
                          f"für <b>{percent}% Rabatt</b>.</p>"
                          f"<p>Gültig bis <b>{ends_at.date().isoformat()}</b>. Viel Spaß beim Einkaufen!</p>")
                    _send_bilingual(subj, to, en, de)
                    emailed += 1
                except Exception as e:
                    errors.append({"user": u.id, "email": to, "error": str(e)})
            except Exception as e:
                errors.append({"user": u.id, "email": u.email, "error": str(e)})

        return Response({
            "ok": True, "count": len(candidates), "created": created, "reused": reused,
            "emailed": emailed, "errors": errors,
            "window": {"starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat()},
            "percent": percent, "valid_days": valid_days,
        }, status=200)

# -----------------------------------------------------------------------------
# Guest order lookup + invoice download (signed)
# -----------------------------------------------------------------------------
class GuestOrderLookupAPIView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        number = (request.data.get("order_number") or "").strip()
        if not email or not number:
            return Response({"detail": "email and order_number are required."}, status=400)

        order = Order.objects.filter(email__iexact=email, number__iexact=number).first()
        if not order:
            order = Order.objects.filter(email__iexact=email, pk__iexact=number.replace("ORD-", "")).first()
        if not order:
            return Response({"detail": "No order found for this email and order number."}, status=404)

        items_data = []
        for it in order.items.all():
            items_data.append({
                "product_id": it.product_id,
                "name": it.product_name,  # uses snapshot property
                "quantity": it.quantity,
                "unit_price": f"{it.unit_price:.2f}",
                "line_total": f"{it.line_total():.2f}",
            })

        invoice_url = None
        if hasattr(order, "invoice") and order.invoice and order.invoice.pdf:
            invoice_url = build_guest_invoice_download_url(order)

        data = {
            "order_id": order.id,
            "order_number": order.number,
            "status": order.status,
            "payment_method": getattr(order, "payment_method_label", None) or order.payment_method,
            "total_price": f"{order.total_price:.2f}",
            "currency": "EUR",
            "created_at": order.created_at,
            "shipping": {
                "first_name": order.ship_first_name,
                "last_name": order.ship_last_name,
                "line1": order.ship_line1,
                "line2": order.ship_line2,
                "postal_code": order.ship_postal,
                "city": order.ship_city,
                "country": order.ship_country,
                "phone": order.ship_phone,
            },
            "items": items_data,
            "invoice_download_url": invoice_url,
        }
        return Response(data, status=200)

class GuestInvoiceDownloadView(APIView):
    """
    Download invoice PDF for guest users via signed token.
    ?token=<signed json>
    Token expires after INVOICE_TOKEN_MAX_AGE seconds.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        token = request.query_params.get("token")
        if not token:
            return Response({"detail": "Missing token."}, status=400)

        try:
            signer = _get_signer()
            raw = signer.unsign(token, max_age=INVOICE_TOKEN_MAX_AGE)
            payload = json.loads(raw)
            order_id = payload.get("order_id")
            invoice_no = payload.get("invoice")
        except Exception:
            return Response({"detail": "Invalid or expired token."}, status=400)

        order = Order.objects.filter(pk=order_id).first()
        if not order:
            return Response({"detail": "Order not found."}, status=404)

        inv = getattr(order, "invoice", None)
        if not inv or not inv.pdf or inv.number != invoice_no:
            return Response({"detail": "Invoice not found."}, status=404)

        return FileResponse(
            inv.pdf.open("rb"),
            as_attachment=True,
            filename=f"{inv.number}.pdf",
            content_type="application/pdf",
        )

# -----------------------------------------------------------------------------
# Payment status update (from frontend echo)
# -----------------------------------------------------------------------------
class OrderPaymentStatusUpdateAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, pk):
        order = get_object_or_404(Order, pk=pk)
        intent_id = (request.data.get("intent_id") or "").strip()
        payment_status = (request.data.get("payment_status") or "").strip().lower()
        gateway_ref = (request.data.get("gateway_ref") or "").strip()

        if not payment_status:
            return Response({"detail": "payment_status is required."}, status=400)

        synonyms_paid = {"succeeded", "completed", "paid", "success"}
        synonyms_processing = {"processing", "pending", "requires_action", "requires_capture"}
        synonyms_failed = {"failed", "canceled", "cancelled", "error"}

        if payment_status in synonyms_paid:
            normalized = "paid"
        elif payment_status in synonyms_processing:
            normalized = "processing"
        elif payment_status in synonyms_failed:
            normalized = "failed"
        else:
            normalized = payment_status  # best-effort

        updates = []
        order.payment_status = normalized
        updates.append("payment_status")

        if gateway_ref or intent_id:
            order.payment_ref = gateway_ref or intent_id
            updates.append("payment_ref")

        if normalized == "paid":
            if order.status != "paid":
                _safe(order.reserve_stock_atomic)
                order.status = "paid"
                updates.append("status")
                order.paid_at = timezone.now()
                updates.append("paid_at")
                order.save(update_fields=list(set(updates)))                
            else:
                order.save(update_fields=list(set(updates)) or ["payment_status"])

        elif normalized == "processing":
            if order.status not in ("paid", "failed"):
                order.status = "processing"
                updates.append("status")
                order.save(update_fields=list(set(updates)))                
            else:
                order.save(update_fields=list(set(updates)) or ["payment_status"])

        else:  # failed
            order.status = "failed"
            updates.append("status")
            order.save(update_fields=list(set(updates)))
            _safe(order.release_stock_atomic)

        return Response({
            "id": order.id,
            "number": order.number,
            "status": order.status,
            "payment_status": getattr(order, "payment_status", ""),
            "paid_at": getattr(order, "paid_at", None),
            "total_price": f"{order.total_price:.2f}",
        }, status=200)