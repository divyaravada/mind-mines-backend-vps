# server/views.py
import random
from os import truncate
import json, re, requests as http
import logging
from itsdangerous import SignatureExpired, BadSignature
from datetime import datetime, timedelta as td
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Any, Dict, List, Mapping, MutableMapping, Iterable, Optional, Tuple, Protocol, TypedDict, runtime_checkable, cast,Type
from django.db.models.functions import TruncDate
from django.core.files.uploadedfile import UploadedFile
from django.utils.datastructures import MultiValueDict
from django.utils.encoding import force_str
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMultiAlternatives, send_mail
from django.db import transaction
from django.db.models import Q, F, Avg, Count
from django.db.models.query import QuerySet
from rest_framework.request import Request
from django.db.models.functions import Lower
from django.http import JsonResponse, Http404, HttpResponse, FileResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string, TemplateDoesNotExist
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.timezone import make_aware,now
from django.views.decorators.csrf import csrf_exempt
from django.db.models.deletion import ProtectedError
from django.db import IntegrityError

from rest_framework import generics, permissions, serializers, filters, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import AllowAny, IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.renderers import JSONRenderer

from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import RefreshToken, AccessToken

from google.oauth2 import id_token
from google.auth.transport.requests import Request as GoogleRequest


# --- local imports (models/serializers/utils) ---
from .models import (
    Address, Cart, LoginOTP, Product, Review, Order, Wishlist,Coupon,
    OnDemandOrderRequest, NewsletterSubscriber, ContactMessage,
    PromotionBanner, StockAlert, ReturnRequest, ReturnAttachment, ProductEvent,
    NaturalStone, NaturalStoneImage, StoneEnquiry, Invoice, OrderItem, MarketingEvent, AbandonedCartReminder,
)
from .serializers import (
    CartSerializer, LoginStartSerializer, LoginVerifySerializer, MyTokenObtainPairSerializer, ProfileSerializer, RegisterSerializer, AccountSerializer,
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
    send_installments_started_email,
    send_admin_new_order_email
)
from .utils.guest_invoices import (
    build_guest_invoice_download_url,
    parse_guest_invoice_token,
)
from server.utils.checkout_tokens import unpack_checkout_payload
from server.utils.installments import build_installments_schedule

import stripe
try:
    # Preferred if available
    from stripe import error as stripe_error  # type: ignore[attr-defined]
except Exception:
    # Fallback shim so code like `except stripe_error.StripeError:` still type-checks
    class _StripeError(Exception):
        pass
    class _SignatureVerificationError(_StripeError):
        pass
    class _DummyStripeErrorNamespace:
        StripeError = _StripeError
        SignatureVerificationError = _SignatureVerificationError
    stripe_error = _DummyStripeErrorNamespace()

# -----------------------------------------------------------------------------
# Setup / constants
# -----------------------------------------------------------------------------
logger = logging.getLogger(__name__)
User = get_user_model()

def _evt(name: str) -> str:
    """
    Resolve MarketingEvent.EventType.<NAME> safely.
    If choices / enum are not present for some reason,
    it falls back to a lowercase string.
    """
    try:
        return getattr(MarketingEvent.EventType, name)
    except Exception:
        return name.lower()

@runtime_checkable
class _HasRequest(Protocol):
    request: Request

def _safe(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception:
        return None

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

def _ensure_invoice_email(order) -> None:
    """
    Make sure the invoice PDF exists and the customer receives it once.
    Safe to call repeatedly; it will no-op if already sent.
    """
    try:
        order.refresh_from_db(fields=["invoice_sent_at"])
    except Exception:
        pass
    if getattr(order, "invoice_sent_at", None):
        return

    _safe(generate_invoice_pdf, order)
    user_locale = getattr(getattr(order.user, "profile", None), "language", "en") or "en"
    recipient = _order_to(order)
    if recipient:
        _safe(send_invoice_email, order, user_locale=user_locale, to=recipient)

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

def _ensure_order_from_checkout_token(pi_data: Mapping[str, Any]) -> Optional[Order]:
    # Already tied to an order?
    # Safely read metadata
    meta = cast(Mapping[str, Any], pi_data.get("metadata") or {})

    # Already tied to an order?
    oid = meta.get("order_id")
    if oid:
        try:
            return Order.objects.get(pk=oid)
        except Order.DoesNotExist:
            return None

    token = meta.get("checkout_token")
    if not token: return None

    # Idempotent: if we already created an order for this PI
    pi_id = cast(Optional[str], pi_data.get("id")) or ""
    if pi_id:
        existing = Order.objects.filter(stripe_pi=pi_id).first()
        if existing:
            return existing

    try:
        payload = unpack_checkout_payload(token)
    except Exception:
        return None

    ser = OrderCreateSerializer(
        data={
            "email": payload.get("email"),
            "payment_method": (meta.get("pm") or "unknown"),
            "coupon_code": payload.get("coupon_code") or "",
            "items": payload.get("items") or [],
            "shipping": payload.get("shipping") or {},
        },
        context={"request": None},
    )
    try:
        ser.is_valid(raise_exception=True)
    except Exception:
        return None

    with transaction.atomic():
        order: Order = cast(Order, ser.save())
        if pi_id:
            order.payment_ref = pi_id
            order.stripe_pi = pi_id
        order.status = "pending"
        order.payment_status = "pending"
        order.save(update_fields=["payment_ref", "stripe_pi", "status", "payment_status"])
        return order
    
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
    def has_object_permission(self, request, view, obj):  # type: ignore[override]
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return False
        return bool(getattr(obj, "user_id", None) == getattr(user, "id", None) or getattr(user, "is_staff", False))

def _client_ip(request) -> str:
    try:
        xff = request.META.get("HTTP_X_FORWARDED_FOR")
        if xff:
            return xff.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR", "") or ""
    except Exception:
        return ""


def _make_otp_code() -> str:
    # 6-digit numeric OTP
    return f"{random.randint(0, 999999):06d}"


class LoginStartAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        ser = LoginStartSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        email = ser.validated_data["email"].strip().lower()
        password = ser.validated_data["password"]

        # authenticate uses your USERNAME_FIELD=email
        user = authenticate(request, username=email, password=password)

        # Don’t leak whether user exists
        if not user:
            return Response({"ok": False, "detail": "Invalid credentials."}, status=400)

        # If you want to block login unless verified:
        # (your system uses is_active for verification)
        if not getattr(user, "is_active", True):
            return Response({"ok": False, "detail": "Account not verified."}, status=403)

        # Optional: basic rate-limit (one active OTP per user)
        # Invalidate older, unconsumed OTPs
        try:
            LoginOTP.objects.filter(user=user, consumed_at__isnull=True, expires_at__gt=timezone.now()).update(
                expires_at=timezone.now()
            )
        except Exception:
            pass

        code = _make_otp_code()
        otp = LoginOTP.create_for_user(
            user=user,
            code=code,
            ip=_client_ip(request),
            user_agent=(request.META.get("HTTP_USER_AGENT", "") or "")[:255],
            ttl_minutes=10,
        )

        # Send email (EN/DE). Reuse your existing helper.
        try:
            subj = "Your login code / Ihr Login-Code"
            en = f"<p>Your MindMines login code is: <b>{code}</b></p><p>Valid for 10 minutes.</p>"
            de = f"<p>Ihr MindMines Login-Code lautet: <b>{code}</b></p><p>Gültig für 10 Minuten.</p>"
            _send_bilingual(subj, user.email, en, de)  # ✅ you already have this helper
        except Exception:
            # If email fails, don’t allow login to proceed
            return Response({"ok": False, "detail": "Could not send OTP email."}, status=500)

        payload = {"ok": True, "otp_id": str(otp.id), "detail": "OTP sent to email."}
        if getattr(request, "user", None) and getattr(settings, "DEBUG", False):
            # do NOT expose OTP in production
            payload["dev_code"] = code
        return Response(payload, status=200)


class LoginVerifyAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        ser = LoginVerifySerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        otp_id = ser.validated_data["otp_id"]
        code = str(ser.validated_data["code"]).strip()

        otp = LoginOTP.objects.select_related("user").filter(id=otp_id).first()
        if not otp:
            return Response({"ok": False, "detail": "Invalid OTP session."}, status=400)

        if otp.is_consumed():
            return Response({"ok": False, "detail": "OTP already used."}, status=400)

        if otp.is_expired():
            return Response({"ok": False, "detail": "OTP expired."}, status=400)

        # attempts limit
        if otp.attempts >= 5:
            return Response({"ok": False, "detail": "Too many attempts."}, status=429)

        if not otp.check_code(code):
            otp.attempts = otp.attempts + 1
            otp.save(update_fields=["attempts"])
            return Response({"ok": False, "detail": "Invalid code."}, status=400)

        # success -> consume and issue tokens
        otp.consumed_at = timezone.now()
        otp.save(update_fields=["consumed_at"])

        user = otp.user

        refresh = RefreshToken.for_user(user)
        access = refresh.access_token

        return Response(
            {
                "ok": True,
                "refresh": str(refresh),
                "access": str(access),
                "user": {
                    "id": user.pk,
                    "email": getattr(user, "email", ""),
                    "username": getattr(user, "username", "") or "",
                },
            },
            status=200,
  
        )
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

    def get_queryset(self) -> QuerySet[Address]:  # type: ignore[override]
        return (
            Address.objects
            .filter(user=self.request.user)
            .order_by("-is_default", "-updated_at", "-id")
        )

    def perform_create(self, serializer: AddressSerializer) -> None:
        addr = serializer.save()  # AddressSerializer.create pulls user from context
        # First address becomes default
        if not Address.objects.filter(user=self.request.user).exclude(pk=addr.pk).exists():
            addr.set_default_for_user()

    def get_serializer_context(self) -> dict[str, Any]:
        ctx = super().get_serializer_context()
        ctx["request"] = self.request
        return ctx

class AddressDetailAPIView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = AddressSerializer

    def get_queryset(self) -> QuerySet[Address]:  # type: ignore[override]
        # Only allow the logged-in user to access their own addresses
        return Address.objects.filter(user=self.request.user)

    def get_serializer_context(self) -> dict[str, Any]:
        ctx = super().get_serializer_context()
        ctx["request"] = self.request
        return ctx

    def destroy(self, request, *args, **kwargs):
        """
        Custom delete:
        - Only deletes addresses belonging to the current user (via get_queryset()).
        - If the address is default, automatically assigns a new default if available.
        - If the address is protected by related objects (e.g. Orders with PROTECT),
          return a JSON error instead of a 500.
        """
        instance = self.get_object()
        user = instance.user
        was_default = instance.is_default

        try:
            # this may raise ProtectedError / IntegrityError
            instance.delete()
        except (ProtectedError, IntegrityError):
            return Response(
                {
                    "code": "address_in_use",
                    "detail": (
                        "This address is used by existing data (e.g. orders) and "
                        "cannot be deleted. / Diese Adresse wird von vorhandenen "
                        "Daten (z.B. Bestellungen) verwendet und kann nicht gelöscht werden."
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # After successful delete: if it was default, pick a new default for that user
        if was_default:
            new_default = (
                Address.objects.filter(user=user)
                .order_by("-updated_at", "-created_at")
                .first()
            )
            if new_default:
                new_default.set_default_for_user()

        return Response(status=status.HTTP_204_NO_CONTENT)

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
             # Verify Google ID token
            idinfo: Mapping[str, Any] = id_token.verify_oauth2_token(
                token, GoogleRequest(), settings.SOCIAL_AUTH_GOOGLE_OAUTH2_KEY
            )
            if idinfo.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
                return Response({"detail": "Invalid issuer"}, status=400)

            email: str = idinfo["email"]
            username = email.split("@")[0]
            full_name = str(idinfo.get("name", "") or "")

            user, created = User.objects.get_or_create(
                email=email, defaults={"username": username, "is_active": True}
            )

            # Profile may not exist on stubbed AbstractUser; guard with getattr
            prof = getattr(user, "profile", None)
            if created and prof is not None:
                setattr(prof, "full_name", full_name)
                prof.save(update_fields=["full_name"])

            # Avoid stub issue on .access_token: create explicitly
            refresh = RefreshToken.for_user(user)
            access = AccessToken.for_user(user)
            return Response({
                "refresh": str(refresh),
                "access": str(access),
                "user": {
                    "id": user.pk,  # pk avoids 'id' stub complaints
                    "email": getattr(user, "email", ""),
                    "username": getattr(user, "username", ""),
                },
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
        email = (request.data.get("email") or "").strip().lower()

        if not email:
            return Response(
                {
                    "code": "email_required",
                    "detail": (
                        "E-mail is required. / "
                        "E-Mail-Adresse wird benötigt."
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            # USER NOT REGISTERED
            return Response(
                {
                    "code": "user_not_found",
                    "detail": (
                        "No account found with this e-mail address. / "
                        "Kein Konto mit dieser E-Mail-Adresse gefunden."
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # USER EXISTS → create token + reset link
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)

        frontend_url = getattr(settings, "FRONTEND_URL", "https://mindmines.eu").rstrip("/")
        reset_url = f"{frontend_url}/reset-password/{uidb64}/{token}/"

        context = {
            "reset_link": reset_url,
            "user": user,
        }

        # Fallback plain-text (bilingual)
        plain_message = (
            "Reset your password using the link below:\n"
            f"{reset_url}\n\n"
            "Wenn Sie Ihr Passwort zurücksetzen möchten, verwenden Sie den folgenden Link:\n"
            f"{reset_url}\n"
        )

        try:
            html_message = render_to_string(
                "registration/password_reset_email.html",
                context,
            )
        except TemplateDoesNotExist:
            html_message = None

        send_mail(
            subject="Reset your password / Passwort zurücksetzen",
            message=plain_message,
            html_message=html_message,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", settings.EMAIL_HOST_USER),
            recipient_list=[email],
            fail_silently=False,
        )

        return Response(
            {
                "code": "reset_email_sent",
                "detail": (
                    "Password reset e-mail has been sent. / "
                    "Die E-Mail zum Zurücksetzen des Passworts wurde gesendet."
                ),
            },
            status=status.HTTP_200_OK,
        )



class CustomPasswordResetConfirmView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    def post(self, request, uidb64, token, *args, **kwargs):
        try:
            uid = force_str(urlsafe_base64_decode(uidb64))
            user = User.objects.get(pk=uid)
        except Exception:
            return Response(
                {
                    "code": "invalid_link",
                    "error": (
                        "Invalid reset link. / "
                        "Ungültiger Link zum Zurücksetzen."
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not default_token_generator.check_token(user, token):
            return Response(
                {
                    "code": "invalid_or_expired_token",
                    "error": (
                        "Invalid or expired token. / "
                        "Ungültiger oder abgelaufener Token."
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        password1 = request.data.get("new_password1")
        password2 = request.data.get("new_password2")

        if not password1 or not password2:
            return Response(
                {
                    "code": "password_required",
                    "error": (
                        "Both password fields are required. / "
                        "Beide Passwortfelder sind erforderlich."
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if password1 != password2:
            return Response(
                {
                    "code": "password_mismatch",
                    "error": (
                        "Passwords do not match. / "
                        "Die Passwörter stimmen nicht überein."
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.set_password(password1)
        user.save()

        return Response(
            {
                "code": "password_reset_success",
                "detail": (
                    "Password has been reset successfully. / "
                    "Das Passwort wurde erfolgreich zurückgesetzt."
                ),
            },
            status=status.HTTP_200_OK,
        )

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
        data = cast(MutableMapping[str, Any], request.data.copy())

        user = getattr(request, "user", None)
        if user and getattr(user, "is_authenticated", False):
            email = (getattr(user, "email", "") or "").strip()
            if not email:
                return Response({"email": ["Your account has no email on file."]}, status=400)
            data["email"] = email
        else:
            if not data.get("email"):
                return Response({"email": ["This field is required."]}, status=400)

        serializer = StoneEnquirySerializer(data=data)
        serializer.is_valid(raise_exception=True)

        # Tell the type checker this is a StoneEnquiry instance
        enquiry: StoneEnquiry = cast(StoneEnquiry, serializer.save())
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
    def _parse_multi(self, key: str) -> List[str]:
        req = cast(_HasRequest, self)
        q = req.request.query_params

        vals = q.getlist(key) or ([q.get(key)] if q.get(key) else [])
        out: List[str] = []
        for v in vals:
            if not v or not isinstance(v, str):
                continue
            s = v.strip()
            # support JSON arrays like ["a","b"]
            if s.startswith("[") and s.endswith("]"):
                try:
                    arr = json.loads(s)
                    out.extend([str(x).strip().lower() for x in arr if str(x).strip()])
                    continue
                except Exception:
                    pass
            out.extend([p.strip().lower() for p in s.split(",") if p.strip()])
        return out

    def _to_decimal(self, val: Any) -> Optional[Decimal]:
        """
        Normalize EU/US price formats → integer euros (Decimal)

        Examples:
        "1186"        -> 1186
        "1.186,50€"   -> 1186
        "1,186.50€"   -> 1186
        "1186.00"     -> 1186
        "53€"         -> 53
        """
        if val is None:
            return None

        s = str(val).strip()
        if not s:
            return None

        # remove currency & spaces
        s = s.replace("€", "").replace(" ", "")

        try:
            # If both separators exist, decide decimal separator by LAST occurrence
            if "." in s and "," in s:
                if s.rfind(",") > s.rfind("."):
                    # EU: 1.186,50 → remove thousands ".", decimal ","
                    s = s.replace(".", "").replace(",", ".")
                else:
                    # US: 1,186.50 → remove thousands ","
                    s = s.replace(",", "")

            # Only comma present
            elif "," in s:
                # Decimal comma if ends with ,dd
                if re.search(r",\d{1,2}$", s):
                    s = s.replace(".", "").replace(",", ".")
                else:
                    # thousands comma
                    s = s.replace(",", "")

            # Only dot present
            elif "." in s:
                # Decimal dot if ends with .dd
                if not re.search(r"\.\d{1,2}$", s):
                    # thousands dot
                    s = s.replace(".", "")

            d = Decimal(s)

            # ✅ drop cents → integer euros
            return Decimal(int(d))

        except (InvalidOperation, ValueError, TypeError):
            return None

    def _parse_price_range(self) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        req = cast(_HasRequest, self)
        q = req.request.query_params

        min_in = q.get("min_price") or q.get("price_min") or q.get("price_gte")
        max_in = q.get("max_price") or q.get("price_max") or q.get("price_lte")
        rng = q.get("price") or q.get("range")

        if rng:
            s = str(rng).strip().lower()
            m = re.match(r"^\s*(\d+(?:\.\d{1,2})?)\s*-\s*(\d+(?:\.\d{1,2})?)\s*$", s)
            if m:
                min_in = min_in or m.group(1)
                max_in = max_in or m.group(2)
            m = re.match(r"^\s*(below|under|lt|lte)\s+(\d+(?:\.\d{1,2})?)\s*$", s)
            if m:
                max_in = max_in or m.group(2)
            m = re.match(r"^\s*(above|over|gt|gte)\s+(\d+(?:\.\d{1,2})?)\s*$", s)
            if m:
                min_in = min_in or m.group(2)

        lo = self._to_decimal(min_in)
        hi = self._to_decimal(max_in)
        if lo is not None and hi is not None and lo > hi:
            lo, hi = hi, lo
        return lo, hi

    def filtered_products_qs(self) -> QuerySet[Product]:
        qs: QuerySet[Product] = Product.objects.all()

        req = cast(_HasRequest, self)
        category = req.request.query_params.get("category")
        if category:
            qs = qs.filter(category=category)

        materials = self._parse_multi("material")
        if materials:
            qs = qs.filter(material__in=materials)

        colors = self._parse_multi("color")
        if colors:
            qs = qs.filter(color__in=colors)

        min_price, max_price = self._parse_price_range()
        if min_price is not None:
            qs = qs.filter(price_gross__gte=min_price)
        if max_price is not None:
            qs = qs.filter(price_gross__lte=max_price)

        # --- ordering mapping for frontend values ---
        ordering_param = req.request.query_params.get("ordering")
        if ordering_param:
            mapping = {
                "price": "price_gross",
                "-price": "-price_gross",
                "title": "title_en",
                "-title": "-title_en",
                "created": "created_at",
                "-created": "-created_at",
            }
            mapped = mapping.get(ordering_param.strip(), None)
            if mapped:
                qs = qs.order_by(mapped)

        return qs.annotate(
            reviews_count=Count("reviews", distinct=True),
            average_rating=Avg("reviews__rating"),
        )
    
class ProductListCreate(ProductFilterMixin, generics.ListCreateAPIView):
    serializer_class = ProductSerializer
    permission_classes = [AllowAny]
    authentication_classes = []
    filter_backends = [filters.SearchFilter]
    search_fields = ["title_en", "title_de"]
    parser_classes = [MultiPartParser, FormParser]

    # helps DRF & type checkers
    queryset = Product.objects.all()

    def get_queryset(self) -> QuerySet[Any]:  # type: ignore[override]
        return self.filtered_products_qs()

class ProductListView(ProductFilterMixin, generics.ListAPIView):
    serializer_class = ProductSerializer
    permission_classes = [AllowAny]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["title_en", "title_de"]
    ordering_fields = ("created_at", "price_gross", "id", "title_en", "title_de")
    ordering = ("-created_at", "id")
    # helps DRF & type checkers
    queryset = Product.objects.all()

    def get_queryset(self) -> QuerySet[Any]:  # type: ignore[override]
        return self.filtered_products_qs()

class ProductDetailView(generics.RetrieveAPIView):
    serializer_class = ProductSerializer
    permission_classes = [AllowAny]
    # helps DRF and type checkers
    queryset = Product.objects.all()

    # No return annotation => no incompatible override warning
    def get_queryset(self) -> QuerySet[Any]:  # type: ignore[override]
        return Product.objects.annotate(
            reviews_count=Count("reviews", distinct=True),
            average_rating=Avg("reviews__rating"),
        )

class ProductSuggestList(generics.ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = ProductMiniSerializer
    filter_backends = [filters.SearchFilter]
    pagination_class = None
    
    # helps DRF & type checkers
    queryset = Product.objects.all()

    def get_queryset(self) -> QuerySet[Any]:  # type: ignore[override]
        # Support both DRF Request (query_params) and Django HttpRequest (GET)
        req = cast(Request, self.request)
        qparams: Mapping[str, Any] = getattr(req, "query_params", None) or req.GET

        term = (qparams.get("search") or "").strip()
        if not term:
            return Product.objects.none()

        return (
            Product.objects
            .filter(Q(title_en__icontains=term) | Q(title_de__icontains=term))
            .order_by(Lower("title_en"))[:10]
        )

class ProductReviewListCreate(generics.ListCreateAPIView):
    parser_classes = [MultiPartParser, FormParser]
    
    # helps DRF & type checkers
    queryset = Review.objects.all()

    def get_permissions(self):
        return [permissions.IsAuthenticated()] if self.request.method == "POST" else [permissions.AllowAny()]
    
    def get_queryset(self) -> QuerySet[Any]:  # type: ignore[override]
        # use .get to avoid Optional subscript warnings in stubs
        pk = self.kwargs.get("pk")
        return Review.objects.filter(product_id=pk).order_by("-created_at")

    def get_serializer_class(self) -> Type[Any]:  # type: ignore[override]
        # DRF stubs sometimes declare Never; return Type[Any] and ignore override
        return ReviewWriteSerializer if self.request.method == "POST" else ReviewSerializer
    
    def get_serializer_context(self) -> Dict[str, Any]:
        return {"request": self.request}

    def create(self, request, *args, **kwargs) -> Response:
        write_ser = ReviewWriteSerializer(data=request.data)
        write_ser.is_valid(raise_exception=True)

        # safe kwarg access for type checkers
        pk = kwargs.get("pk")
        product = get_object_or_404(Product, pk=pk)

        review_user = request.user
        # validated_data is Dict after is_valid(); cast to keep Pylance happy
        vd: Dict[str, Any] = cast(Dict[str, Any], getattr(write_ser, "validated_data", {}) or {})

        if getattr(request.user, "is_staff", False):
            maybe_user = vd.pop("user", None)
            if maybe_user:
                review_user = maybe_user

        instance = Review.objects.create(
            user=review_user,
            product=product,
            rating=vd["rating"],
            message=vd.get("message", "") or "",
            image=vd.get("image"),
            author_name=vd.get("author_name", "") or "",
            author_avatar=vd.get("author_avatar"),
        )
        read_ser = ReviewSerializer(instance, context={"request": request})
        headers = self.get_success_headers(read_ser.data)
        return Response(read_ser.data, status=201, headers=headers)

class ReviewListAPIView(generics.ListAPIView):
    serializer_class = ReviewSerializer
    permission_classes = [permissions.IsAuthenticated]
     # help DRF & type checkers
    queryset = Review.objects.all()

    def get_queryset(self) -> QuerySet[Any]:  # type: ignore[override]
        return Review.objects.filter(user=self.request.user).order_by("-created_at")
    def get_serializer_context(self) -> dict[str, Any]:
        return {"request": self.request}

class ReviewDetailAPIView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Review.objects.all()
    permission_classes = [permissions.IsAuthenticated, IsOwnerOrAdmin]
    parser_classes = [MultiPartParser, FormParser]
    def get_serializer_class(self) -> Type[Any]:  # type: ignore[override]
        return ReviewWriteSerializer if self.request.method in ("PUT", "PATCH") else ReviewSerializer
    def get_serializer_context(self) -> dict[str, Any]:
        return {"request": self.request}

#-----------------------------------------------------------------------------
# Admin Product Management
#-----------------------------------------------------------------------------
class ProductDemandSummaryAPIView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        # date range (optional)
        dfrom = request.query_params.get("from")
        dto = request.query_params.get("to")

        def _parse(dt: str | None):
            if not dt:
                return None
            try:
                return make_aware(datetime.fromisoformat(dt))
            except ValueError:
                return None
            
        start = _parse(dfrom)
        end = _parse(dto)

        ev_filter = Q()
        if start:
            ev_filter &= Q(events__created_at__gte=start)
        if end:
            ev_filter &= Q(events__created_at__lte=end)

        # Annotate then project to dicts
        qs = (
            Product.objects
            .annotate(
                views=Count("events", filter=ev_filter & Q(events__action="view")),
                wishlists=Count("events", filter=ev_filter & Q(events__action="wishlist")),
                add_to_carts=Count("events", filter=ev_filter & Q(events__action="add_to_cart")),
            )
            .annotate(
                demand_score=F("add_to_carts") * 3 + F("wishlists") * 2 + F("views")
            )
            .values(
                "id", "title_en", "views", "wishlists", "add_to_carts", "demand_score"
            )
            .order_by("-demand_score")
        )

        limit = int(request.query_params.get("limit") or 20)
        rows: List[Dict[str, Any]] = list(qs[:limit])

        data = [
            {
                "product_id": row["id"],
                "title": row.get("title_en") or f"Product {row['id']}",
                "views": int(row.get("views") or 0),
                "wishlists": int(row.get("wishlists") or 0),
                "add_to_carts": int(row.get("add_to_carts") or 0),
                "demand_score": int(row.get("demand_score") or 0),
            }
            for row in rows
        ]
        return Response({"results": data})
    
class ProductDemandTimeseriesAPIView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        dfrom = request.query_params.get("from")
        dto = request.query_params.get("to")
        start = make_aware(datetime.fromisoformat(dfrom)) if dfrom else None
        end = make_aware(datetime.fromisoformat(dto)) if dto else None
        limit = int(request.query_params.get("limit") or 5)

        base = ProductEvent.objects.all()
        if start: base = base.filter(created_at__gte=start)
        if end:   base = base.filter(created_at__lte=end)

        # Find top products by weighted score in the window
        top = (
            base.values("product_id")
            .annotate(
                views=Count("id", filter=Q(action="view")),
                wishlists=Count("id", filter=Q(action="wishlist")),
                add_to_carts=Count("id", filter=Q(action="add_to_cart")),
            )
            .annotate(score=F("add_to_carts")*3 + F("wishlists")*2 + F("views"))
            .order_by("-score")[:limit]
        )
        top_ids = [row["product_id"] for row in top]

        # Daily buckets per product
        daily = (
            base.filter(product_id__in=top_ids)
            .annotate(day=TruncDate("created_at"))
            .values("product_id", "day")
            .annotate(
                views=Count("id", filter=Q(action="view")),
                wishlists=Count("id", filter=Q(action="wishlist")),
                add_to_carts=Count("id", filter=Q(action="add_to_cart")),
            )
            .order_by("day", "product_id")
        )

        # Shape the payload: {labels:[...days], series:[{product_id, title, data:[scores]}]}
        days = sorted({row["day"].isoformat() for row in daily})
        title_map = dict(Product.objects.filter(id__in=top_ids).values_list("id", "title_en"))

        # init series map
        series_map = {
            pid: {"product_id": pid, "title": title_map.get(pid, str(pid)), "data": [0]*len(days)}
            for pid in top_ids
        }
        day_index = {d:i for i, d in enumerate(days)}
        for row in daily:
            idx = day_index[row["day"].isoformat()]
            score = row["add_to_carts"]*3 + row["wishlists"]*2 + row["views"]
            series_map[row["product_id"]]["data"][idx] = int(score)

        return Response({"labels": days, "series": list(series_map.values())})   

# -----------------------------------------------------------------------------
# Cart
# -----------------------------------------------------------------------------
from django.db import transaction
from django.utils import timezone

def _get_or_create_active_cart(request):
    user = request.user if request.user.is_authenticated else None
    token = request.data.get("cart_token") or request.query_params.get("cart_token")

    qs = Cart.objects.filter(is_active=True)

    # ---- Logged-in user ----
    if user:
        # Prefer user cart
        user_cart = qs.filter(user=user).order_by("-updated_at").first()

        # If token exists, there might be a guest cart we can merge
        guest_cart = qs.filter(cart_token=token).order_by("-updated_at").first() if token else None

        if user_cart and token and not user_cart.cart_token:
            # keep token on user cart for easier debugging/admin lookup
            user_cart.cart_token = token
            user_cart.save(update_fields=["cart_token", "updated_at"])

        # Merge guest cart into user cart if both exist and they are different carts
        if user_cart and guest_cart and guest_cart.id != user_cart.id:
            _merge_carts(target=user_cart, source=guest_cart)
            return user_cart

        if user_cart:
            return user_cart

        # No user cart yet: if guest cart exists, attach it to user
        if guest_cart:
            guest_cart.user = user
            if token and not guest_cart.cart_token:
                guest_cart.cart_token = token
            guest_cart.save(update_fields=["user", "cart_token", "updated_at"])
            return guest_cart

        # Create new user cart
        return Cart.objects.create(user=user, cart_token=token or None)

    # ---- Guest ----
    if token:
        return qs.filter(cart_token=token).order_by("-updated_at").first() or Cart.objects.create(cart_token=token)

    raise ValueError("cart_token required for guests")


@transaction.atomic
def _merge_carts(target: Cart, source: Cart) -> None:
    """
    Move/merge items from source cart into target cart.
    If same product exists, sum quantities and keep target price/name/image.
    """
    # Load items efficiently
    source_items = list(source.items.all())
    if not source_items:
        source.is_active = False
        source.save(update_fields=["is_active", "updated_at"])
        return

    # Build target map by product_id
    target_items = {it.product_id: it for it in target.items.select_for_update().all()}

    for it in source_items:
        existing = target_items.get(it.product_id)
        if existing:
            existing.qty = int(existing.qty) + int(it.qty)
            existing.save(update_fields=["qty"])
        else:
            it.pk = None
            it.cart = target
            it.save()

    # deactivate source cart so admin sees only one
    source.is_active = False
    source.save(update_fields=["is_active", "updated_at"])


class CartSyncView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        try:
            cart = _get_or_create_active_cart(request)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        data = {
            "currency": request.data.get("currency") or "EUR",
            "is_active": True,
            "cart_token": request.data.get("cart_token"),
        }

        # ✅ only include items if client sent it
        if "items" in request.data:
            data["items"] = request.data.get("items") or []

        ser = CartSerializer(instance=cart, data=data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data, status=status.HTTP_200_OK)
               
class AdminCartListView(generics.ListAPIView):
    """
    Admin view to see all active carts, with items.
    Filters: ?active=true/false & ?q=email_or_token
    """
    serializer_class = CartSerializer
    permission_classes = [permissions.IsAdminUser]

    queryset = Cart.objects.all()
    

    def get_queryset(self) -> QuerySet[Any]:  # type: ignore[override]
        request = cast(Request, self.request)

        qs = (
            Cart.objects.all()
            .order_by("-updated_at")
            .prefetch_related("items__product")
            .select_related("user")
        )

        active = request.query_params.get("active")
        if active in ("true", "false"):
            qs = qs.filter(is_active=(active == "true"))

        q = request.query_params.get("q")
        if q:
            qs = qs.filter(Q(cart_token__icontains=q) | Q(user__email__icontains=q))

        return qs


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

        # 👇 NEW: log marketing event
        try:
            MarketingEvent.objects.create(
                user=request.user if getattr(request, "user", None) and request.user.is_authenticated else None,
                session_id=(request.data.get("session_id") or "").strip()[:64],
                event_type=MarketingEvent.EventType.NEWSLETTER_SUBSCRIBE,
                url_path=(request.data.get("url_path") or "").strip()[:512],
                referrer=(request.data.get("referrer") or "").strip()[:512],
                utm_source=(request.data.get("utm_source") or "").strip(),
                utm_medium=(request.data.get("utm_medium") or "").strip(),
                utm_campaign=(request.data.get("utm_campaign") or "").strip(),
                utm_term=(request.data.get("utm_term") or "").strip(),
                utm_content=(request.data.get("utm_content") or "").strip(),
                extra={"newsletter_email": obj.email},
            )
        except Exception:
            logger.exception("Failed to create MarketingEvent for newsletter subscribe")

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
    queryset = Wishlist.objects.all()

    def get_queryset(self) -> QuerySet[Any]:  # type: ignore[override]
        return Wishlist.objects.filter(user=self.request.user).order_by("id")
    
    def perform_create(self, serializer):
        wl: Wishlist = serializer.save(user=self.request.user)
        # 👇 NEW: track wishlist_add event
        try:
            MarketingEvent.objects.create(
                user=self.request.user,
                session_id=(self.request.data.get("session_id") or "").strip()[:64],
                event_type=_evt("WISHLIST_ADD"),
                product=wl.product,
                url_path=(self.request.data.get("url_path") or "").strip()[:512],
                utm_source=(self.request.data.get("utm_source") or "").strip(),
                utm_medium=(self.request.data.get("utm_medium") or "").strip(),
                utm_campaign=(self.request.data.get("utm_campaign") or "").strip(),
                extra={"wishlist_id": wl.id},
            )
        except Exception:
            logger.exception("Failed to create MarketingEvent for wishlist_add")

class WishlistDetailView(generics.DestroyAPIView):
    serializer_class = WishlistSerializer
    permission_classes = [IsAuthenticated]
    queryset = Wishlist.objects.all()

    def get_object(self) -> Wishlist:  # type: ignore[override]
        return get_object_or_404(
            Wishlist,
            user=self.request.user,
            product_id=self.kwargs["pk"],
        )

# -----------------------------------------------------------------------------
# Orders (read/create/invoice)
# -----------------------------------------------------------------------------
class OrderListAPIView(generics.ListAPIView):
    serializer_class = OrderReadSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None
    queryset = Order.objects.all()

    def get_queryset(self) -> QuerySet[Any]:  # type: ignore[override]
        return Order.objects.filter(user=self.request.user).order_by("-created_at")

class OrderCreateView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        ser = OrderCreateSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)
        with transaction.atomic():
            order = ser.save()

        # ✅ send email to admin after order is successfully created
        send_admin_new_order_email(order)

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
    queryset = Order.objects.all()

    def get_queryset(self) -> QuerySet[Any]:  # type: ignore[override]
        return Order.objects.filter(user=self.request.user)
    def retrieve(self, request, *args, **kwargs):
        ser = self.get_serializer(self.get_object(), context={"request": request})
        return Response(ser.data)

class OrderInvoiceAPIView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request, pk):
        try:
            order = (
                Order.objects
                .select_related("invoice")  # avoids extra query when present
                .get(pk=pk, user=request.user)
            )
        except Order.DoesNotExist:
            raise Http404("Order not found")

        # Access related object in a type-checker-friendly way
        inv: Optional[Invoice] = cast(Optional[Invoice], getattr(order, "invoice", None))  # type: ignore[attr-defined]

        # If missing or missing file, (re)generate and refetch
        if not inv or not getattr(inv, "pdf", None):
            generate_invoice_pdf(order)
            # refresh the relation
            try:
                order.refresh_from_db(fields=[])  # ensure relation is up to date
            except Exception:
                pass
            inv = cast(Optional[Invoice], getattr(order, "invoice", None))  # type: ignore[attr-defined]

        if not inv or not getattr(inv, "pdf", None):
            raise Http404("Invoice not available.")

        filename = inv.number or f"invoice_{order.pk}"
        return FileResponse(
            inv.pdf.open("rb"),
            content_type="application/pdf",
            as_attachment=True,
            filename=f"{filename}.pdf" if not filename.endswith(".pdf") else filename,
        )

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

        vd: Dict[str, Any] = cast(Dict[str, Any], getattr(s, "validated_data", {}) or {})
        code = str(vd.get("code") or "").strip()
        items: List[Dict[str, Any]] = cast(List[Dict[str, Any]], vd.get("items") or [])
        email = str(vd.get("email") or "").strip()  # ✅ NEW

        if not code:
            return Response({"ok": False, "error": "Code is required"}, status=400)
        if not items:
            return Response({"ok": False, "error": "No items provided"}, status=400)

        subtotal = Decimal("0.00")
        for it in items:
            pid_raw = it.get("product_id")
            if pid_raw is None:
                return Response({"ok": False, "error": "Each item must include product_id"}, status=400)
            try:
                pid = int(pid_raw)
            except (TypeError, ValueError):
                return Response({"ok": False, "error": "product_id must be an integer"}, status=400)

            qty_raw = it.get("qty", 1)
            try:
                qty = int(qty_raw)
            except (TypeError, ValueError):
                qty = 1
            if qty <= 0:
                return Response({"ok": False, "error": "qty must be >= 1"}, status=400)

            p = get_object_or_404(Product, pk=pid)
            unit = getattr(p, "final_price_gross", None) or p.price_gross
            unit = Decimal(str(unit))
            subtotal += unit * qty

        coup = Coupon.objects.filter(code__iexact=code).first()
        if not coup:
            return Response({"ok": False, "error": "Coupon not found"}, status=404)

        user_or_none = (
            request.user
            if getattr(request, "user", None) and getattr(request.user, "is_authenticated", False)
            else None
        )

        # ✅ UPDATED: pass email for email-targeted coupons (guest)
        ok, reason = coup.can_apply(user_or_none, items, subtotal, email=email)
        if not ok:
            return Response({"ok": False, "error": reason}, status=400)

        discount = coup.discount_amount(user_or_none, items, subtotal)
        total = (subtotal - discount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        return Response(
            {
                "ok": True,
                "code": coup.code,
                "kind": coup.kind,
                "value": str(coup.value),
                "discount": f"{discount:.2f}",
                "subtotal": f"{subtotal:.2f}",
                "total": f"{total:.2f}",
            }
        )
    
class ActivePromotionAPIView(APIView):
    permission_classes = [AllowAny]
    renderer_classes = [JSONRenderer]

    def get(self, request):
        qs = PromotionBanner.objects.filter(
            is_enabled=True,
            start_at__lte=now(),
            end_at__gte=now(),
        ).order_by("-priority", "-updated_at", "-id")
        data = PromotionSerializer(qs, many=True, context={"request": request}).data
        return Response({"results": data})

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
    # 1. Decode UID safely
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
    except (TypeError, ValueError, OverflowError, UnicodeDecodeError):
        return Response({"detail": "Invalid user ID"}, status=400)

    # 2. Fetch user
    try:
        user = User.objects.get(pk=uid)
    except User.DoesNotExist:
        return Response({"detail": "Invalid user ID"}, status=400)

    # 3. Already active?
    if user.is_active:
        return Response({"detail": "Account already verified"}, status=200)

    # 4. Validate token
    if default_token_generator.check_token(user, token):
        user.is_active = True
        user.save()
        try:
            subj = "Welcome to MindMines / Willkommen bei MindMines"
            en = (
                "Welcome! Your email has been verified successfully.<br/>"
                "You can now sign in and start shopping. Have fun!<br/>"
                "<a href='https://mindmines.eu'>mindmines.eu</a>"
            )
            de = (
                "Willkommen! Ihre E-Mail wurde erfolgreich verifiziert.<br/>"
                "Sie können sich jetzt anmelden und einkaufen. Viel Spaß!<br/>"
                "<a href='https://mindmines.eu'>mindmines.eu</a>"
            )
            _send_bilingual(subj, user.email, f"<p>{en}</p>", f"<p>{de}</p>")
        except Exception:
            logger.exception("Failed to send happy-shopping email after verification")

        return Response({"detail": "Email verified successfully"}, status=200)

    # 5. Token invalid or expired
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
                order: Order = cast(Order, ser.save())
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

METHOD_MINS = {
    "giropay": Decimal("1.00"),   # giropay generally requires >= €1.00
    "klarna":  Decimal("1.00"),   # Klarna commonly requires >= €1.00 in many regions
}

def _amount_cents(value: Decimal) -> int:
    return int((Decimal(value) * Decimal("100")).quantize(Decimal("1")))

def _get_or_create_stripe_customer(user: Optional[Any] = None, email: Optional[str] = None) -> Optional[str]:
    """
    Create/reuse Stripe customer for authenticated checkout.
    Guests can just be None for a one-shot PI.
    """
    if not (user and getattr(user, "is_authenticated", False)):
        return None

    prof = getattr(user, "profile", None)
    existing = getattr(prof, "stripe_customer_id", None)
    if existing:
        return cast(str, existing)

    # Build strictly-typed strings (no Nones)
    email_str: str = str((getattr(user, "email", None) or email or "")).strip()
    first = str(getattr(user, "first_name", "") or "").strip()
    last  = str(getattr(user, "last_name", "") or "").strip()
    name_str: str = f"{first} {last}".strip() or (email_str or f"User {getattr(user, 'id', '')}")

    # metadata must be Dict[str, str]
    meta: Dict[str, str] = {"user_id": str(getattr(user, "id", ""))}

    customer = stripe.Customer.create(
        email=email_str,          # always a str
        name=name_str,            # always a str
        metadata=meta,            # Dict[str, str]
    )

    if prof and hasattr(prof, "stripe_customer_id"):
        prof.stripe_customer_id = customer.id  # type: ignore[attr-defined]
        prof.save(update_fields=["stripe_customer_id"])

    return customer.id

def stripe_client():
    stripe.api_key = settings.STRIPE_SECRET_KEY
    stripe.set_app_info(name="MindMines Payment", version="1.0.0")
    return stripe

def eur_cents(amount: Decimal) -> int:
    # Quantize to 2dp, convert to Stripe's integer amount
    q = Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(q * 100)

# ---------------------------
# STRIPE: immediate one-shot (card)
# ---------------------------

class StripeCreateIntent(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        order_id = request.data.get("order_id")

        # Path 1: Reuse existing order
        if order_id:
            try:
                with transaction.atomic():
                    order: Order = cast(Order, Order.objects.select_for_update().get(pk=order_id))

                    if order.total_price is None or Decimal(order.total_price) <= Decimal("0.00"):
                        return Response({"detail": "Total must be > 0."}, status=400)
                    if order.status not in ("pending", "processing"):
                        return Response({"detail": f"Order not payable in status '{order.status}'"}, status=400)

                    if order.stripe_pi:
                        intent = stripe.PaymentIntent.retrieve(order.stripe_pi)
                    else:
                        create_kwargs = {
                            "amount": _amount_cents(Decimal(order.total_price)),
                            "currency": getattr(settings, "STRIPE_CURRENCY", "eur"),
                            "payment_method_types": ["card"],
                            "description": f"Order {order.number or order.id} — MindMines Payment",
                            "metadata": {"order_id": str(order.id)},
                        }
                        email = (order.email or "").strip()
                        if email:
                            create_kwargs["receipt_email"] = email

                        intent = stripe.PaymentIntent.create(**create_kwargs)
                        order.payment_method = "card"
                        order.status = "pending"
                        order.payment_ref = intent.id
                        order.stripe_pi = intent.id
                        order.save(update_fields=["payment_method", "status", "payment_ref", "stripe_pi"])

                return Response({"client_secret": intent.client_secret, "order_id": order.id}, status=200)

            except Order.DoesNotExist:
                return Response({"detail": "Order not found."}, status=404)
            except stripe_error.StripeError as e:
                return Response({"detail": f"Stripe error: {str(e)}"}, status=502)

        # Path 2: Create order + PI in one call
        ser = OrderCreateSerializer(data=request.data, context={"request": request})
        try:
            ser.is_valid(raise_exception=True)
        except serializers.ValidationError as ve:
            return Response({"detail": "Order validation failed", "errors": ve.detail}, status=400)

        try:
            with transaction.atomic():
                order: Order = cast(Order, ser.save())
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
            create_kwargs = {
                "amount": _amount_cents(total),
                "currency": getattr(settings, "STRIPE_CURRENCY", "eur"),
                "payment_method_types": ["card"],
                "description": f"Order {order.number or order.id} — MindMines Payment",
                "metadata": {"order_id": str(order.id)},
            }
            email = (order.email or "").strip()
            if email:
                create_kwargs["receipt_email"] = email

            intent = stripe.PaymentIntent.create(**create_kwargs)
        except stripe_error.StripeError as e:
            order.status = "failed"
            order.save(update_fields=["status"])
            return Response({"detail": f"Stripe error: {str(e)}"}, status=502)

        order.payment_ref = intent.id
        order.stripe_pi = intent.id
        order.save(update_fields=["payment_ref", "stripe_pi"])

        return Response({"client_secret": intent.client_secret, "order_id": order.id}, status=201)

# ---------------------------
# STRIPE: immediate one-shot ( klarna / giropay )
# ---------------------------
class _PaymentIntent(TypedDict, total=False):
    id: str
    client_secret: str
    amount: int
    currency: str
    payment_method_types: List[str]

class StripeCreateRedirectIntent(APIView):
    """
    Creates a PaymentIntent for Klarna/giropay. If order_id is provided, reuse that order.
    Otherwise create a NEW order from the posted payload (guest-safe).
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        
        method = (request.data.get("method") or "").strip().lower()
        if method not in {"klarna", "giropay"}:
            return Response({"detail": "method must be 'klarna' or 'giropay'."}, status=400)

        order_id = request.data.get("order_id")
        if not order_id:
            return Response(
                {"detail": "order_id is required."},
                status=400,
            )

        try:
            with transaction.atomic():
                order = Order.objects.select_for_update().get(pk=order_id)
        except Order.DoesNotExist:
            return Response({"detail": "Order not found."}, status=404)

        # sanity checks
        if order.total_price is None or Decimal(order.total_price) <= Decimal("0.00"):
            return Response({"detail": "Total must be > 0."}, status=400)
        if order.status not in ("pending", "processing"):
            return Response({"detail": f"Order not payable in status '{order.status}'"}, status=400)

        
        currency = getattr(settings, "STRIPE_CURRENCY", "eur")
        email_str: str = (order.email or "").strip()  # always a str

        # Reuse PI if it already matches this method/amount/currency; else create new
        intent: Optional[_PaymentIntent] = None
        try:
            if order.stripe_pi:
                current: Any = stripe.PaymentIntent.retrieve(order.stripe_pi)
                if isinstance(current, dict):
                    pmt_types = current.get("payment_method_types") or []
                    if (
                        isinstance(pmt_types, list)
                        and pmt_types == [method]
                        and current.get("currency") == currency
                        and int(current.get("amount") or -1) == _amount_cents(Decimal(order.total_price))
                    ):
                        intent = cast(_PaymentIntent, current)
        except stripe_error.StripeError:
            intent = None

        if not intent:
            try:
                kwargs: Dict[str, Any] = dict(
                    amount=_amount_cents(Decimal(order.total_price)),
                    currency=currency,
                    payment_method_types=[method],
                    description=f"Order {order.number or order.id} — MindMines Payment",
                    metadata={"order_id": str(order.id)},
                )
                # Only include receipt_email if we actually have one (avoids Optional[str])
                if email_str:
                    kwargs["receipt_email"] = email_str

                created: Any = stripe.PaymentIntent.create(**kwargs)
                # stripe returns dict-like; narrow to our minimal shape
                intent = cast(_PaymentIntent, created)
            except stripe_error.StripeError as e:
                return Response({"detail": f"Stripe error: {str(e)}"}, status=502)

            order.payment_method = method
            order.payment_ref = intent.get("id", "")  # safe even if missing
            order.stripe_pi = intent.get("id", "")
            order.save(update_fields=["payment_method", "payment_ref", "stripe_pi"])

        # Final guard for type-checkers and runtime
        if not intent or "client_secret" not in intent or "id" not in intent:
            return Response({"detail": "Failed to obtain PaymentIntent."}, status=502)

        return Response(
            {"client_secret": intent["client_secret"], "order_id": order.id},
            status=200,
        )
       

# ---------------------------
# STRIPE: card installments (authenticated only)
# ---------------------------

class StripeCreateSetupIntent(APIView):
    """
    Create a SetupIntent so the client can save a card and obtain a payment_method_id.
    Works for guests and users. For guests, we create/get a Stripe customer by email.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        # always keep these as plain strings for type checkers
        email_str: str = (request.data.get("email") or "").strip()
        user = request.user if getattr(request, "user", None) and request.user.is_authenticated else None

        try:
            customer_id: Optional[str] = _get_or_create_stripe_customer(user=user, email=email_str or None)

            # Build kwargs without Optional values
            kwargs: Dict[str, Any] = {
                "payment_method_types": ["card"],
                "usage": "off_session",  # saving for future charges
            }
            if customer_id:  # only include when not None
                kwargs["customer"] = customer_id

            si = stripe.SetupIntent.create(**kwargs)

            return Response(
                {"client_secret": si.client_secret, "customer_id": customer_id},
                status=201,
            )
        except stripe_error.StripeError as e:
            return Response({"detail": f"Stripe error: {str(e)}"}, status=502)
        
class StripeCardInstallmentsView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        # 1) Validate months early, as int
        months_raw = request.data.get("installments_months") or request.data.get("months") or 0
        try:
            months = int(months_raw)
        except (TypeError, ValueError):
            months = 0
        if months not in (3, 6, 9):
            return Response({"installments_months": ["Choose 3, 6, or 9 months for card installments."]}, status=400)

        # 2) Prepare serializer input
        data = request.data.copy()
        data["payment_method"] = "card_installments"
        data["installments_months"] = months

        ser = OrderCreateSerializer(data=data, context={"request": request})
        ser.is_valid(raise_exception=True)

        # 3) Cast validated_data so Pylance knows it’s a dict
        vd: Dict[str, Any] = cast(Dict[str, Any], getattr(ser, "validated_data", {}) or {})

        # 4) Create order with explicit type
        try:
            with transaction.atomic():
                order: Order = cast(Order, ser.create(vd))
                order.payment_method = "card_installments"
                order.installments_months = months
                order.status = "processing"
                order.confirmation_sent_at = timezone.now()
                order.save(update_fields=["payment_method", "installments_months", "status", "confirmation_sent_at"])
        except Exception as e:
            return Response({"detail": f"Order creation failed: {e}"}, status=500)

        # 5) Require payment_method_id as a non-empty string
        pm_id = str(request.data.get("payment_method_id") or "").strip()
        if not pm_id:
            return Response({"detail": "payment_method_id is required"}, status=400)

        try:
            # Retrieve PM and ensure we have a customer id (string)
            pm = stripe.PaymentMethod.retrieve(pm_id)
            customer_id_any: Any = pm.get("customer")
            customer_id: Optional[str] = customer_id_any if isinstance(customer_id_any, str) and customer_id_any else None

            if not customer_id:
                customer_id = _get_or_create_stripe_customer(
                    user=request.user if getattr(request, "user", None) and request.user.is_authenticated else None,
                    email=order.email,
                )
                # Only attach if we truly have a string
                if customer_id:
                    stripe.PaymentMethod.attach(pm_id, customer=customer_id)

            # Set default PM (guard the call so we never pass None)
            if customer_id:
                stripe.Customer.modify(
                    customer_id,
                    invoice_settings={"default_payment_method": pm_id},
                )

            # 6) Build server-authoritative schedule for emails/response
            amounts_eur, dates = build_installments_schedule(Decimal(order.total_price), months, first_now=True)

            # 7) Stripe subscription schedule (last item carries remainder)
            total_cents = int(Decimal(order.total_price) * 100)
            base = total_cents // months
            remainder = total_cents - base * months

            def mk_price(amount_int: int, label: str):
                return stripe.Price.create(
                    unit_amount=amount_int,
                    currency=getattr(settings, "STRIPE_CURRENCY", "eur"),
                    recurring={"interval": "month"},
                    product_data={"name": label},
                )

            now_ = timezone.now()
            def ts(dt): return int(dt.timestamp())

            title_base = f"Installments for Order {order.number or order.id}"
            phases: list[dict[str, Any]] = []

            if remainder:
                if months > 1:
                    price_base = mk_price(base, f"{title_base} (monthly)")
                    phases.append({
                        "items": [{"price": price_base.id, "quantity": 1}],
                        "end_date": ts(now_ + td(days=30 * (months - 1))),
                    })
                price_last = mk_price(base + remainder, f"{title_base} (final)")
                phases.append({
                    "items": [{"price": price_last.id, "quantity": 1}],
                    "end_date": ts(now_ + td(days=30 * months)),
                })
            else:
                price_all = mk_price(base, f"{title_base} (monthly)")
                phases.append({
                    "items": [{"price": price_all.id, "quantity": 1}],
                    "end_date": ts(now_ + td(days=30 * months)),
                })

            # 8) Create the subscription schedule (customer_id must be a str)
            if not customer_id:
                return Response({"detail": "Could not determine a Stripe customer for this payment method."}, status=400)

            schedule = stripe.SubscriptionSchedule.create(
                customer=customer_id,
                start_date="now",
                end_behavior="cancel",
                phases=cast(Any, phases),
                default_settings={
                    "default_payment_method": pm_id,
                    "collection_method": "charge_automatically",
                },
                metadata={"order_id": str(order.id)},
            )

            order.payment_ref = schedule.id
            order.save(update_fields=["payment_ref"])

            # 9) Create invoice PDF + send installments email; suppress generic “processing”
            try:
                generate_invoice_pdf(order)
            except Exception:
                pass

            type(order).objects.filter(pk=order.pk).update(confirmation_sent_at=timezone.now())
            setattr(order, "confirmation_sent_at", timezone.now())

            user_locale = getattr(getattr(order.user, "profile", None), "language", "en") or "en"
            send_installments_started_email(
                order,
                months=months,
                amounts_eur=amounts_eur,
                dates=dates,
                user_locale=user_locale,
                to=(order.email or getattr(getattr(order, "user", None), "email", None)),
                attach_invoice=True,
            )

            return Response({
                "order_id": order.id,
                "schedule_id": schedule.id,
                "months": months,
                "amounts": [str(a) for a in amounts_eur],
                "dates": [timezone.localtime(d).strftime("%Y-%m-%d") for d in dates],
            }, status=201)

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
            order: Order = cast(Order, ser.save())
            order.payment_method = "sepa_debit"
            order.status = "pending"
            order.save(update_fields=["payment_method", "status"])

        # we *require* email for mandate
        email = (order.email or "").strip()
        if not email:
            return Response({"detail": "Email is required for SEPA payments."}, status=400)

        try:
            customer_id: Optional[str] = _get_or_create_stripe_customer(
                user=request.user if getattr(request, "user", None) and request.user.is_authenticated else None,
                email=email,
            )

            create_kwargs: Dict[str, Any] = {
                "amount": _amount_cents(Decimal(order.total_price)),
                "currency": getattr(settings, "STRIPE_CURRENCY", "eur"),
                "payment_method_types": ["sepa_debit"],
                "description": f"Order {order.number or order.id} — MindMines Payment",
                "metadata": {"order_id": str(order.id)},
                "receipt_email": email,
                "confirm": False,
            }
            # Only pass customer if we actually have a string
            if customer_id:
                create_kwargs["customer"] = customer_id

            intent = stripe.PaymentIntent.create(**create_kwargs)

        except stripe_error.StripeError as e:
            order.status = "failed"
            order.save(update_fields=["status"])
            return Response({"detail": str(e)}, status=502)

        order.payment_ref = intent.id
        order.stripe_pi = intent.id
        order.save(update_fields=["payment_ref", "stripe_pi"])

        return Response({"clientSecret": intent.client_secret, "order_id": order.id}, status=201)

# ---------------------------
# STRIPE: SEPA installments (DE only)
# ---------------------------
class StripeCreateSepaSetupIntent(APIView):
    """
    Create a SetupIntent for SEPA so the client can save a mandate and get payment_method_id.
    Works for guests and users.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        email = (request.data.get("email") or "").strip() or None
        user = request.user if getattr(request, "user", None) and request.user.is_authenticated else None
        try:
            customer_id: Optional[str] = _get_or_create_stripe_customer(user=user, email=email)

            # Build kwargs and add "customer" only if it's a non-empty string
            create_kwargs: Dict[str, Any] = {
                "payment_method_types": ["sepa_debit"],
                "usage": "off_session",
            }
            if customer_id:
                create_kwargs["customer"] = customer_id

            si = stripe.SetupIntent.create(**create_kwargs)

            return Response(
                {"client_secret": si.client_secret, "customer_id": customer_id},
                status=201
            )
        except stripe_error.StripeError as e:
            return Response({"detail": f"Stripe error: {str(e)}"}, status=502)
        
class StripeSepaInstallmentsView(APIView):
    """
    SEPA installments with a saved `payment_method_id` or a freshly collected one.
    Works for guests and authenticated users.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        # --- validate inputs early ---
        months_raw = request.data.get("installments_months") or request.data.get("months") or 0
        try:
            months = int(months_raw)
        except (TypeError, ValueError):
            months = 0

        country = (request.data.get("country") or "").upper()[:2]

        if months not in (3, 6, 9):
            return Response({"installments_months": ["Choose 3, 6, or 9 months for SEPA installments."]}, status=400)
        if country != "DE":
            return Response({"detail": "Installments are available in DE only."}, status=400)
        
        # --- run serializer with explicit inputs for validation ---
        data = request.data.copy()
        data["payment_method"] = "sepa_installments"
        data["installments_months"] = months

        ser = OrderCreateSerializer(data=data, context={"request": request})
        ser.is_valid(raise_exception=True)

        # help the type checker: validated_data is a Dict
        vd: Dict[str, Any] = cast(Dict[str, Any], ser.validated_data or {})

        # --- create order inside transaction; tell the checker its type is Order ---
        with transaction.atomic():
            order: Order = cast(Order, ser.create(vd))
            order.payment_method = "sepa_installments"
            order.installments_months = months
            order.status = "processing"
            order.confirmation_sent_at = timezone.now()
            order.save(update_fields=["payment_method","installments_months","status","confirmation_sent_at"])

        pm_id = request.data.get("payment_method_id")
        if not pm_id:
            return Response({"detail": "payment_method_id is required"}, status=400)

        try:
            # Retrieve PM; ensure we end up with a string customer_id
            pm: Dict[str, Any] = stripe.PaymentMethod.retrieve(pm_id)  # type: ignore[no-redef]
            cid_any: Any = pm.get("customer")
            customer_id: Optional[str] = cid_any if isinstance(cid_any, str) and cid_any else None

            # If not attached, create/reuse a Stripe customer and attach PM
            if not customer_id:
                customer_id = _get_or_create_stripe_customer(
                    user=request.user if getattr(request, "user", None) and request.user.is_authenticated else None,
                    email=order.email,
                )
                if customer_id:  # only call attach with a real string
                    stripe.PaymentMethod.attach(pm_id, customer=customer_id)

            # Set default PM (only if we truly have a string)
            if customer_id:
                stripe.Customer.modify(customer_id, invoice_settings={"default_payment_method": pm_id})

            # --- compute server-authoritative schedule (for emails/response) ---
            amounts_eur, dates = build_installments_schedule(Decimal(order.total_price), months, first_now=True)

            # --- build subscription schedule phases; keep types happy ---
            total_cents = int(Decimal(order.total_price) * 100)
            base = total_cents // months
            remainder = total_cents - base * months

            def mk_price(amount_int: int, label: str):
                return stripe.Price.create(
                    unit_amount=amount_int,
                    currency=getattr(settings, "STRIPE_CURRENCY", "eur"),
                    recurring={"interval": "month"},
                    product_data={"name": label},
                )

            now_ = timezone.now()
            def ts(dt): return int(dt.timestamp())

            title_base = f"SEPA installments for Order {order.number or order.id}"

            # Build phases as a plain list of dicts, then cast to Any for strict stubs
            raw_phases: List[Dict[str, Any]] = []
            if remainder:
                if months > 1:
                    base_price = mk_price(base, f"{title_base} (monthly)")
                    raw_phases.append({
                        "items": [{"price": base_price.id, "quantity": 1}],
                        "end_date": ts(now_ + td(days=30 * (months - 1))),
                    })
                last_price = mk_price(base + remainder, f"{title_base} (final)")
                raw_phases.append({
                    "items": [{"price": last_price.id, "quantity": 1}],
                    "end_date": ts(now_ + td(days=30 * months)),
                })
            else:
                only_price = mk_price(base, f"{title_base} (monthly)")
                raw_phases.append({
                    "items": [{"price": only_price.id, "quantity": 1}],
                    "end_date": ts(now_ + td(days=30 * months)),
                })


            # SubscriptionSchedule.create requires "customer" to be a str — guard for None
            if not customer_id:
                return Response({"detail": "Could not determine a Stripe customer for this payment method."}, status=400)

            schedule = stripe.SubscriptionSchedule.create(
                customer=customer_id,
                start_date="now",
                end_behavior="cancel",
                phases=cast(Any, raw_phases),  # satisfy strict stripe type stubs
                default_settings={
                    "default_payment_method": pm_id,
                    "collection_method": "charge_automatically",
                },
                metadata={"order_id": str(order.id)},
            )

            order.payment_ref = schedule.id
            order.save(update_fields=["payment_ref"])

            # Generate invoice PDF & send **installments schedule** email
            try:
                generate_invoice_pdf(order)
            except Exception:
                pass

            # prevent the generic “processing” email later
            type(order).objects.filter(pk=order.pk).update(confirmation_sent_at=timezone.now())
            setattr(order, "confirmation_sent_at", timezone.now())

            user_locale = getattr(getattr(order.user, "profile", None), "language", "en") or "en"
            send_installments_started_email(
                order,
                months=months,
                amounts_eur=amounts_eur,
                dates=dates,
                user_locale=user_locale,
                to=(order.email or getattr(getattr(order, "user", None), "email", None)),
                attach_invoice=True,
            )

            return Response({
                "order_id": order.id,
                "schedule_id": schedule.id,
                "months": months,
                "amounts": [str(a) for a in amounts_eur],
                "dates": [timezone.localtime(d).strftime("%Y-%m-%d") for d in dates],
            }, status=201)

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

    # ---- define oid once so it’s always bound ----
    oid = None
    meta = data.get("metadata") if isinstance(data, dict) else None
    if isinstance(meta, dict):
        oid = meta.get("order_id")

    def _mark_paid(order: Order, pi_data: dict):
        changed = False
        if order.status != "paid":
            order.status = "paid"
            order.payment_status = "paid"
            order.paid_at = timezone.now()
            changed = True

        # final charge id if available
        if not getattr(order, "payment_id", None):
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
            order.save(update_fields=["status", "payment_status", "paid_at", "payment_id"])

            # 👇 NEW: marketing conversion event
            try:
                MarketingEvent.objects.create(
                    user=order.user if getattr(order, "user", None) else None,
                    session_id="",
                    event_type=_evt("ORDER_COMPLETED"),
                    order=order,
                    extra={
                        "total_price": str(order.total_price),
                        "payment_method": getattr(order, "payment_method", ""),
                        "gateway": pi_data.get("payment_method_types"),
                    },
                )
            except Exception:
                logger.exception("Failed to create MarketingEvent for order_completed")

        # reserve inventory for fulfilled orders
        _safe(order.reserve_stock_atomic)
        _ensure_invoice_email(order)

    # payment_intent.succeeded => normal card, klarna, sofort, giropay
    if etype == "payment_intent.succeeded":
        oid = (data.get("metadata") or {}).get("order_id")
        try:
            with transaction.atomic():
                if oid:
                    order = Order.objects.select_for_update().get(pk=oid)
                else:
                    order = _ensure_order_from_checkout_token(data)
                    if not order: return HttpResponse(status=200)
                _mark_paid(order, data)
        except Order.DoesNotExist:
            pass

    elif etype == "invoice.payment_succeeded":
        if oid:
            try:
                with transaction.atomic():
                    order = Order.objects.select_for_update().get(pk=oid)
                    _mark_paid(order, data)
            except Order.DoesNotExist:
                pass

    # payment_intent.processing => SEPA direct debit / waiting bank
    elif etype == "payment_intent.processing":
        try:
            with transaction.atomic():
                if oid:
                    order = Order.objects.select_for_update().get(pk=oid)
                else:
                    order = _ensure_order_from_checkout_token(data)
                    if not order: return HttpResponse(status=200)
                if order.status != "processing":
                    order.status = "processing"
                    order.payment_status = "processing"
                    order.save(update_fields=["status","payment_status"])
        except Order.DoesNotExist:
            pass

    elif etype in ("payment_intent.payment_failed", "invoice.payment_failed"):
        try:
            with transaction.atomic():
                if oid:
                    order = Order.objects.select_for_update().get(pk=oid)
                else:
                    order = _ensure_order_from_checkout_token(data)
                    if not order: return HttpResponse(status=200)
                order.status = "failed"
                order.payment_status = "failed"
                order.save(update_fields=["status","payment_status"])
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
        # Query directly instead of accessing reverse attribute
        inv: Optional[Invoice] = Invoice.objects.filter(order=order).first()
        if not inv or not getattr(inv, "pdf", None):
            return Response({"detail": "Invoice not available."}, status=404)

        filename = inv.number or f"invoice_{order.pk}"
        return FileResponse(
            inv.pdf.open("rb"),
            as_attachment=True,
            filename=f"{filename}.pdf" if not filename.endswith(".pdf") else filename,
        )

# -----------------------------------------------------------------------------
# Returns / Cancellations
# -----------------------------------------------------------------------------
def _can_return(order: Order) -> Tuple[bool, Optional[str]]:
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
    def post(self, request: Request, pk: int, *args, **kwargs) -> Response:
        order: Order = get_object_or_404(
            Order.objects.select_related("user"), pk=pk, user=request.user
        )
        # ❌ Block if there is already a return request for this order+user
        already_returned = ReturnRequest.objects.filter(
            order=order,
            user=request.user,
            request_type="return",
        ).exists()
        if already_returned:
            return Response(
                {"detail": "You already submitted a return request for this order."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        allowed, reason = _can_return(order)
        if not allowed:
            return Response({"detail": reason or ""}, status=status.HTTP_403_FORBIDDEN)

        # 🔧 Give Pylance concrete types
        data: Mapping[str, Any] = cast(Mapping[str, Any], request.data)
        files: MultiValueDict[str, UploadedFile] = cast(
            MultiValueDict[str, UploadedFile], request.FILES
        )

        message = str(data.get("message") or "").strip()

        rr: ReturnRequest = ReturnRequest.objects.create(
            order=order,
            user=request.user,
            request_type="return",
            message=message,
        )

        for f in files.getlist("files"):
            ReturnAttachment.objects.create(request=rr, file=f)

        email_sent = False
        email_err: Optional[str] = None
        try:
            send_return_request_emails(rr)
            email_sent = True
        except Exception as e:
            email_err = f"{e.__class__.__name__}: {e}"
        # ✅ Return flags for frontend
        has_return_request = True
        has_cancel_request = ReturnRequest.objects.filter(
            order=order,
            user=request.user,
            request_type="cancel",
        ).exists()
        return Response(
            {
                "ok": True,
                "request_id": rr.pk,
                "email_sent": email_sent,
                "error": email_err,
                "has_return_request": has_return_request,
                "has_cancel_request": has_cancel_request,
            },
            status=201,
        )

class OrderCancelRequestAPIView(APIView):
    permission_classes = [IsAuthenticated]
    def post(self, request, pk: int):
        order = get_object_or_404(Order, pk=pk, user=request.user)
        # ❌ Block if a cancel request already exists
        already_cancelled = ReturnRequest.objects.filter(
            order=order,
            user=request.user,
            request_type="cancel",
        ).exists()
        if already_cancelled:
            return Response(
                {"detail": "You already submitted a cancellation request for this order."},
                status=400,
            )
        allowed = False
        if order.status in ("pending", "processing", "shipped"):
            allowed = True
        elif order.status == "delivered" and order.delivered_at:
            CANCELLATION_WINDOW_DAYS = getattr(settings, "CANCELLATION_WINDOW_DAYS", RETURNS_WINDOW_DAYS)
            delta = timezone.now() - order.delivered_at
            allowed = (delta.days <= CANCELLATION_WINDOW_DAYS)
        if not allowed:
            return Response(
                {"detail": "This order can no longer be cancelled."}, status=400
            )

        msg = (request.data.get("message") or "").strip()
        rr = ReturnRequest.objects.create(
            order=order,
            user=request.user,
            request_type="cancel",
            message=msg,
        )
        for f in request.FILES.getlist("files"):
            ReturnAttachment.objects.create(request=rr, file=f)

        email_sent = False
        try:
            send_cancel_request_emails(rr)
            email_sent = True
        except Exception:
            logger.exception("Failed to send cancel emails for rr=%s", rr.pk)

        # ✅ Return flags for frontend
        has_cancel_request = True
        has_return_request = ReturnRequest.objects.filter(
            order=order,
            user=request.user,
            request_type="return",
        ).exists()

        return Response(
            {
                "id": rr.pk,
                "status": rr.status,
                "request_type": rr.request_type,
                "email_sent": email_sent,
                "has_cancel_request": has_cancel_request,
                "has_return_request": has_return_request,
            },
            status=201,
        )

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

        # 👇 NEW: marketing event
        try:
            MarketingEvent.objects.create(
                user=user if getattr(user, "is_authenticated", False) else None,
                session_id=(self.request.data.get("session_id") or "").strip()[:64],
                event_type=_evt("ON_DEMAND_REQUEST"),
                product=instance.product,
                url_path=(self.request.data.get("url_path") or "").strip()[:512],
                utm_source=(self.request.data.get("utm_source") or "").strip(),
                utm_medium=(self.request.data.get("utm_medium") or "").strip(),
                utm_campaign=(self.request.data.get("utm_campaign") or "").strip(),
                extra={
                    "on_demand_id": instance.id,
                    "quantity": getattr(instance, "quantity", None),
                    "email": getattr(instance, "email", None),
                },
            )
        except Exception:
            logger.exception("Failed to create MarketingEvent for on_demand_request")

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

        # 👇 NEW: marketing event for subscription
        try:
            MarketingEvent.objects.create(
                user=self.request.user if getattr(self.request, "user", None) and self.request.user.is_authenticated else None,
                session_id=(self.request.data.get("session_id") or "").strip()[:64],
                event_type=_evt("STOCK_ALERT_SUBSCRIBE"),
                product=product,
                url_path=(self.request.data.get("url_path") or "").strip()[:512],
                utm_source=(self.request.data.get("utm_source") or "").strip(),
                utm_medium=(self.request.data.get("utm_medium") or "").strip(),
                utm_campaign=(self.request.data.get("utm_campaign") or "").strip(),
                extra={"stock_alert_id": alert.id, "email": alert.email},
            )
        except Exception:
            logger.exception("Failed to create MarketingEvent for stock_alert_subscribe")

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

    def _deactivate_and_track(self, alert: StockAlert, request):
        if alert.is_active:
            alert.is_active = False
            alert.save(update_fields=["is_active"])
        # 👇 NEW: marketing event
        try:
            MarketingEvent.objects.create(
                user=None,
                session_id=(request.GET.get("session_id") or "").strip()[:64],
                event_type=_evt("STOCK_ALERT_UNSUBSCRIBE"),
                product=alert.product,
                extra={"stock_alert_id": alert.id, "email": alert.email},
            )
        except Exception:
            logger.exception("Failed to create MarketingEvent for stock_alert_unsubscribe")

    def get(self, request, *args, **kwargs):
        alert = get_object_or_404(self.get_queryset(), unsubscribe_token=kwargs.get("unsubscribe_token"))
        self._deactivate_and_track(alert, request)
        return Response({"ok": True, "unsubscribed": True}, status=200)

    def delete(self, request, *args, **kwargs):
        alert = get_object_or_404(self.get_queryset(), unsubscribe_token=kwargs.get("unsubscribe_token"))
        self._deactivate_and_track(alert, request)
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
        ends_at = run_dt + timezone.timedelta(days=valid_days)

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

        
        created = 0
        reused = 0
        emailed = 0
        errors = []

        def ensure_coupon_for_user(user):
            code = f"BD{mmdd}-{user.id}"
            coup = Coupon.objects.filter(code=code).first()
            if coup:
                # Ensure assignment exists (safe idempotent)
                if hasattr(coup, "target"):
                    coup.target = "users"
                    coup.max_uses = coup.max_uses or 1
                    coup.max_uses_per_user = coup.max_uses_per_user or 1
                    coup.save(update_fields=["target", "max_uses", "max_uses_per_user"])
                if hasattr(coup, "assigned_users"):
                    coup.assigned_users.add(user)
                return coup, False

            coup = Coupon.objects.create(
                code=code,
                kind="percent",
                value=percent,
                active=True,
                starts_at=starts_at,
                ends_at=ends_at,
                is_birthday=True,

                # ✅ NEW
                target="users",
                max_uses=1,
                max_uses_per_user=1,
            )
            coup.assigned_users.add(user)  # ✅ NEW
            return coup, True

        for u in candidates:
            try:
                coup, was_created = ensure_coupon_for_user(u)
                created += 1 if was_created else 0
                reused += 0 if was_created else 1

                # 👇 NEW: marketing event for birthday coupon
                try:
                    MarketingEvent.objects.create(
                        user=u,
                        session_id="",
                        event_type=_evt("BIRTHDAY_COUPON_SENT"),
                        coupon=coup,
                        extra={
                            "percent": percent,
                            "valid_until": ends_at.date().isoformat(),
                            "was_created": was_created,
                        },
                    )
                except Exception:
                    logger.exception("Failed to create MarketingEvent for birthday_coupon_sent")
                    
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
    """
    Public endpoint: look up an order by (email, order_number) and return a summary
    plus a signed invoice download URL (when available).
    """
    permission_classes = [AllowAny]

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        number = (request.data.get("order_number") or "").strip()
        if not email or not number:
            return Response({"detail": "email and order_number are required."}, status=400)

        qs: QuerySet[Order] = Order.objects.filter(email__iexact=email)
        order: Optional[Order] = qs.filter(number__iexact=number).first()
        if not order:
            pk_guess = number.replace("ORD-", "")
            order = qs.filter(pk__iexact=pk_guess).first()
        if not order:
            return Response({"detail": "No order found for this email and order number."}, status=404)

        # -- items helper that never touches unknown attributes statically
        def _iter_items(o: Order) -> Iterable[Any]:
            rel_any: Any = getattr(o, "items", None) or getattr(o, "orderitem_set", None)
            if rel_any is None:
                return []  # no related name known to us at type time
            try:
                return cast(Iterable[Any], rel_any.all())
            except Exception:
                return []

        def _fmt_money(x: Any) -> str:
            try:
                return f"{float(x):.2f}"
            except Exception:
                return "0.00"

        items_data: list[Dict[str, Any]] = []
        for it in _iter_items(order):
            # Safely fetch fields/methods without upsetting the type checker
            product_id = getattr(it, "product_id", None)
            name = (
                getattr(it, "product_name", None)
                or getattr(it, "product_name_snapshot", None)
                or ""
            )
            quantity = getattr(it, "quantity", 0)
            unit_price = _fmt_money(getattr(it, "unit_price", 0))
            line_total_fn = getattr(it, "line_total", None)
            line_total_val = line_total_fn() if callable(line_total_fn) else getattr(it, "line_total", 0)

            items_data.append(
                {
                    "product_id": it.product_id,
                    "name": getattr(it, "product_name", None) or getattr(it, "product_name_snapshot", ""),
                    "quantity": it.quantity,
                    "unit_price": f"{it.unit_price:.2f}",
                    "line_total": f"{it.line_total():.2f}",
                }
            )

        # Invoice URL (optional)
        invoice_url: Optional[str] = None
        inv = getattr(order, "invoice", None)
        if inv is not None and getattr(inv, "pdf", None):
            invoice_url = build_guest_invoice_download_url(order)

        data = {
            "order_id": getattr(order, "id", None),
            "order_number": getattr(order, "number", ""),
            "status": getattr(order, "status", ""),
            "payment_method": getattr(order, "payment_method_label", None) or getattr(order, "payment_method", ""),
            "total_price": _fmt_money(getattr(order, "total_price", 0)),
            "currency": "EUR",
            "created_at": getattr(order, "created_at", None),
            "shipping": {
                "first_name": getattr(order, "ship_first_name", ""),
                "last_name": getattr(order, "ship_last_name", ""),
                "line1": getattr(order, "ship_line1", ""),
                "line2": getattr(order, "ship_line2", ""),
                "postal_code": getattr(order, "ship_postal", ""),
                "city": getattr(order, "ship_city", ""),
                "country": getattr(order, "ship_country", ""),
                "phone": getattr(order, "ship_phone", ""),
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
            order_id, invoice_no = parse_guest_invoice_token(token)
        except SignatureExpired:
            # token is valid but too old
            return Response({"detail": "Invoice link has expired."}, status=400)
        except BadSignature:
            # completely invalid
            return Response({"detail": "Invalid token."}, status=400)
        except Exception:
            return Response({"detail": "Invalid or expired token."}, status=400)

        if not order_id or not invoice_no:
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
            _ensure_invoice_email(order)

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
# -----------------------------------------------------------------------------
# Digital Marketing / Tracking API
# -----------------------------------------------------------------------------
class MarketingEventTrackView(APIView):
    """
    Public endpoint for frontend tracking.

    Example POST body:
    {
      "event_type": "product_view",
      "product_id": 123,
      "order_id": 45,
      "coupon_code": "WINTER24",
      "session_id": "abc123",
      "url_path": "/products/marble-bowl",
      "referrer": "https://google.com",
      "utm_source": "google",
      "utm_medium": "cpc",
      "utm_campaign": "winter24",
      "utm_term": "marble bowl",
      "utm_content": "ad_variant_a",
      "country": "DE",
      "device_type": "desktop",
      "extra": {...}
    }
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request: Request):
        data = request.data

        raw_type = (data.get("event_type") or data.get("type") or "").strip().lower()
        if not raw_type:
            return Response({"detail": "event_type is required."}, status=400)

        # If your model has EventType.choices we validate against it,
        # otherwise we accept anything and just store a string.
        valid_types = None
        try:
            valid_types = {choice[0] for choice in MarketingEvent.EventType.choices}
        except Exception:
            valid_types = None

        if valid_types and raw_type not in valid_types:
            return Response(
                {
                    "detail": (
                        f"Invalid event_type '{raw_type}'. "
                        f"Allowed: {sorted(valid_types)}"
                    )
                },
                status=400,
            )

        # optional relations
        product = None
        order = None
        coupon = None

        pid = data.get("product_id")
        if pid:
            try:
                product = Product.objects.get(pk=int(pid))
            except (Product.DoesNotExist, ValueError, TypeError):
                product = None

        oid = data.get("order_id")
        if oid:
            try:
                order = Order.objects.get(pk=int(oid))
            except (Order.DoesNotExist, ValueError, TypeError):
                order = None

        coupon_code = (data.get("coupon_code") or "").strip()
        if coupon_code:
            coupon = Coupon.objects.filter(code__iexact=coupon_code).first()

        session_id = (
            (data.get("session_id") or data.get("cart_token") or "").strip()[:64]
        )

        # utm params
        utm_source = (data.get("utm_source") or "").strip()
        utm_medium = (data.get("utm_medium") or "").strip()
        utm_campaign = (data.get("utm_campaign") or "").strip()
        utm_term = (data.get("utm_term") or "").strip()
        utm_content = (data.get("utm_content") or "").strip()

        # page context
        url_path = (data.get("url_path") or data.get("path") or "").strip()[:512]
        referrer = (data.get("referrer") or "").strip()[:512]

        # device / geo
        country = (data.get("country") or "").upper()[:2]
        device_type = (data.get("device_type") or "").strip()[:32]
        user_agent = request.META.get("HTTP_USER_AGENT", "")[:256]

        extra = data.get("extra") or {}

        ev = MarketingEvent.objects.create(
            user=request.user if getattr(request, "user", None) and request.user.is_authenticated else None,
            session_id=session_id,
            event_type=raw_type if not valid_types else raw_type,
            product=product,
            order=order,
            coupon=coupon,
            url_path=url_path,
            referrer=referrer,
            utm_source=utm_source,
            utm_medium=utm_medium,
            utm_campaign=utm_campaign,
            utm_term=utm_term,
            utm_content=utm_content,
            country=country,
            device_type=device_type,
            user_agent=user_agent,
            extra=extra if isinstance(extra, dict) else {},
        )

        return Response({"ok": True, "id": ev.id}, status=201)
