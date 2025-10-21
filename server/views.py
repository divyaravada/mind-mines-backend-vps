from decimal import Decimal,ROUND_HALF_UP,InvalidOperation
from dateutil.relativedelta import relativedelta
import json, re
import requests as http
from typing import Optional
from django.utils.timezone import now
from django.db.models.functions import Lower
from rest_framework import permissions,generics, status, filters, throttling, serializers
from rest_framework.authentication import SessionAuthentication, BasicAuthentication
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail, EmailMultiAlternatives
from django.http import FileResponse , Http404,HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string,TemplateDoesNotExist
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.db import transaction
from django.utils import timezone
from datetime import datetime
from django.db.models import Avg, Count, Q



from google.oauth2 import id_token
from google.auth.transport.requests import Request as GoogleRequest

from rest_framework.decorators import api_view, permission_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import AllowAny, IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView



from .pagination import StandardResultsSetPagination
from .serializers import (
    ProfileSerializer, RegisterSerializer, MyTokenObtainPairSerializer,
    ProductSerializer,ProductMiniSerializer, ContactMessageSerializer,ReviewWriteSerializer,
    NewsletterSubscriberSerializer, AddressSerializer, PromotionSerializer,GuestOrderLookupInputSerializer,
    OrderPublicSerializer,
    ReviewSerializer, WishlistSerializer, NaturalStoneSerializer,CouponPreviewIn,
    StoneEnquirySerializer, AccountSerializer, OrderCreateSerializer, OrderReadSerializer, 
)
from .models import (
    User, Product, ContactMessage, NewsletterSubscriber,Promotion,
    Address, Order, Review, Wishlist, NaturalStone, NaturalStoneImage,ReturnRequest,ReturnAttachment
)
from .utils.invoices import generate_invoice_pdf
from .utils.background import fire_and_forget
from .tasks import post_payment_pipeline
from .utils.emails import send_installments_started_email, send_return_request_emails,send_cancel_request_emails,send_order_confirmation, send_bank_transfer_instructions, send_invoice_email,send_sepa_pending_email

import logging
logger = logging.getLogger(__name__)



User = get_user_model()
def _safe(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception:
        return None



# Optional async pipeline used in your code

try:
    from server.tasks import post_payment_pipeline, fire_and_forget
except Exception:
    def fire_and_forget(func, *args, **kwargs):
        _safe(func, *args, **kwargs)
    def post_payment_pipeline(*_, **__):
        pass
class MyTokenObtainPairView(TokenObtainPairView):
    serializer_class = MyTokenObtainPairSerializer

class UserProfileAPIView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        return Response(ProfileSerializer(request.user.profile).data)
    def put(self, request):
        ser = ProfileSerializer(request.user.profile, data=request.data, partial=True)
        if ser.is_valid():
            ser.save()
            return Response(ser.data)
        return Response(ser.errors, status=400)

class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    permission_classes = [AllowAny]
    serializer_class = RegisterSerializer

class IsOwnerOrAdmin(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        return bool(request.user and (obj.user_id == request.user.id or request.user.is_staff))


# ✅ Account view (username/full_name/mobile/image update)
class AccountAPIView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    def get(self, request):
        return Response(AccountSerializer(request.user).data)
    def put(self, request):
        ser = AccountSerializer(instance=request.user, data=request.data, partial=True)
        if ser.is_valid():
            ser.save()
            return Response(ser.data)
        return Response(ser.errors, status=400)

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
            f"Email: {user.email}\n"
            f"Username: {user.username or '-'}\n"
            f"Full name: {full_name or '-'}\n"
            f"User ID: {user.id}\n"
            f"Reason:\n{reason}\n"
        )
        html = (
            f"<p><strong>User requested account deletion.</strong></p>"
            f"<p><b>Email:</b> {user.email}<br/>"
            f"<b>Username:</b> {user.username or '-'}<br/>"
            f"<b>Full name:</b> {full_name or '-'}<br/>"
            f"<b>User ID:</b> {user.id}</p>"
            f"<p><b>Reason:</b><br/>{reason.replace(chr(10), '<br/>')}</p>"
        )

        email = EmailMultiAlternatives(
                subject=subject,
                body=plain,
                from_email=settings.EMAIL_HOST_USER,  # enforce the authenticated mailbox
                to=[admin_to],
                reply_to=[user.email],                # you can still receive replies at user’s email
        )
        email.attach_alternative(html, "text/html")
        email.send(fail_silently=False)

        return Response({"detail": "We received your request."}, status=200)

# -------------------- Addresses --------------------

class AddressListCreateAPIView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AddressSerializer

    def get_queryset(self):
        # default first, then most recent
        return Address.objects.filter(user=self.request.user).order_by("-is_default", "-updated_at", '-id')

    def perform_create(self, serializer):
        addr = serializer.save()  # serializer sets user from request
        # if first ever, force default
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
            new_default = Address.objects.filter(user=user).order_by('-updated_at', '-created_at').first()
            if new_default:
                new_default.set_default_for_user()

class AddressSetDefaultAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def post(self, request, pk):
        addr = get_object_or_404(Address, pk=pk, user=request.user)
        addr.set_default_for_user()
        return Response(AddressSerializer(addr, context={"request": request}).data)


# -------------------- Social Login --------------------

class GoogleLoginAPIView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        token = request.data.get("token")
        if not token:
            return Response({"detail": "Missing token"}, status=400)
        try:
            idinfo = id_token.verify_oauth2_token(
                token,
                GoogleRequest(),
                settings.SOCIAL_AUTH_GOOGLE_OAUTH2_KEY,  # must match frontend client ID exactly
            )
            if idinfo.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
                return Response({"detail": "Invalid issuer"}, status=400)

            email = idinfo["email"]
            username = email.split("@")[0]
            full_name = idinfo.get("name", "")

            user, created = User.objects.get_or_create(
                email=email,
                defaults={"username": username, "is_active": True},
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
            # token parse/verify issues (audience, exp, etc.)
            return Response({"detail": f"Google token invalid: {e}"}, status=400)
        except Exception as e:
            return Response({"detail": f"Auth error: {e}"}, status=400)



# -------------------- Password reset --------------------

class CustomPasswordResetView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    def post(self, request):
        email = request.data.get("email")
        user = User.objects.filter(email=email).first()
        if user:
            uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            reset_url = f"https://mindmines.eu/reset-password/{uidb64}/{token}/"
            message = render_to_string("registration/password_reset_email.html", {
                "reset_link": reset_url, "user": user,
            })
            send_mail(
                subject="Reset Your Password",
                message="Click the link to reset your password.",
                html_message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
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

# -------------------- Natural stones --------------------

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
        """
        - If authenticated: use account email, ignore any client-provided email.
        - If guest: require 'email' in payload.
        """
        data = request.data.copy()
        user = request.user if request.user.is_authenticated else None

        if user:
            if not user.email:
                return Response(
                    {"email": ["Your account has no email on file."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            data["email"] = user.email
        else:
            if not data.get("email"):
                return Response(
                    {"email": ["This field is required."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        serializer = StoneEnquirySerializer(data=data)
        if not serializer.is_valid():
            # You’ll see exactly why you got 400 in the client
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        enquiry = serializer.save()

        # Send confirmation to the user
        try:
            send_mail(
                subject="Thank you for your enquiry",
                message=(
                    f"Dear {enquiry.name},\n\n"
                    f"Thank you for reaching out regarding our natural stone: {enquiry.stone_type}.\n"
                    f"We will get back to you shortly.\n\n"
                    f"Best regards,\nMindMines Team"
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[enquiry.email],
                fail_silently=False,
            )
        except Exception as e:
            logger.exception("Failed sending enquiry confirmation email: %s", e)

        # Notify admin
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
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[admin_to],
                fail_silently=True,
            )
        except Exception:
            logger.exception("Failed sending admin enquiry email")

        return Response(StoneEnquirySerializer(enquiry).data, status=status.HTTP_201_CREATED)


class TestPostView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        return Response({"message": "POST request successful!"}, status=status.HTTP_200_OK)

# -------------------- Products / Reviews --------------------
class ProductFilterMixin:
    def _parse_multi(self, key: str):
        q = self.request.query_params
        vals = q.getlist(key) or ([q.get(key)] if q.get(key) else [])
        out = []
        for v in vals:
            if not v or not isinstance(v, str):
                continue
            s = v.strip()
            # JSON array?
            if s.startswith('[') and s.endswith(']'):
                try:
                    arr = json.loads(s)
                    out.extend([str(x).strip().lower() for x in arr if str(x).strip()])
                    continue
                except Exception:
                    pass
            # comma-separated
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

        min_price, max_price = self._parse_price_range()
        if min_price is not None:
            qs = qs.filter(price__gte=min_price)
        if max_price is not None:
            qs = qs.filter(price__lte=max_price)

        # ✅ Correct reverse name is "reviews"
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
    filter_backends = [filters.SearchFilter]
    search_fields = ['title_en', 'title_de']

    def get_queryset(self):
        return self.filtered_products_qs()

class ProductDetailView(generics.RetrieveAPIView):
    serializer_class = ProductSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        # also annotated for detail
        return Product.objects.annotate(
            reviews_count=Count("reviews", distinct=True),
            average_rating=Avg("reviews__rating"),
        )



# views.py
class ProductSuggestList(generics.ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = ProductMiniSerializer
    filter_backends = [filters.SearchFilter]
    pagination_class = None  # return small list

    def get_queryset(self):
        term = (self.request.query_params.get("search") or "").strip()
        if not term:
            return Product.objects.none()
        return Product.objects.filter(
            Q(title_en__icontains=term) | Q(title_de__icontains=term)
        ).order_by(Lower("title_en")).all()[:10]

class ProductReviewListCreate(generics.ListCreateAPIView):
    """
    GET: anyone can read product reviews
    POST: authenticated users can create;
          admins may optionally set:
            - author_name / author_avatar (cosmetic)
            - user (attribute to any account)
    """
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

        # default to current user
        review_user = request.user

        # admins can attribute review to another user
        if request.user.is_staff:
            maybe_user = write_ser.validated_data.pop("user", None)
            if maybe_user:
                review_user = maybe_user

        instance = Review.objects.create(
            user=review_user,
            product=product,
            rating=write_ser.validated_data["rating"],
            message=write_ser.validated_data["message"],
            image=write_ser.validated_data.get("image"),
            author_name=write_ser.validated_data.get("author_name", ""),
            author_avatar=write_ser.validated_data.get("author_avatar"),
        )

        read_ser = ReviewSerializer(instance, context={"request": request})
        headers = self.get_success_headers(read_ser.data)
        return Response(read_ser.data, status=status.HTTP_201_CREATED, headers=headers)


class ReviewListAPIView(generics.ListAPIView):
    """My reviews (dashboard)"""
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

    # IMPORTANT: let IsOwnerOrAdmin decide; don't re-check owner here
    def perform_update(self, serializer):
        serializer.save()

    def perform_destroy(self, instance):
        instance.delete()

# -------------------- Contact / Newsletter --------------------

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
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[settings.ADMIN_EMAIL],
            fail_silently=True,
        )

class NewsletterSubscriberCreateView(generics.CreateAPIView):
    queryset = NewsletterSubscriber.objects.all()
    serializer_class = NewsletterSubscriberSerializer
    permission_classes = [AllowAny]
    authentication_classes = []  # avoid CSRF/session requirements for this public endpoint

    def create(self, request, *args, **kwargs):
        # normalize early
        email = (request.data.get("email") or "").strip().lower()
        if not email:
            return Response({"email": ["This field is required."]}, status=400)

        # upsert behavior: duplicate -> 200 OK
        obj, created = NewsletterSubscriber.objects.get_or_create(email=email)

        # Send admin notification
        try:
            admin_html = render_to_string(
                "emails/admin_newsletter_notify.html",
                {"email": obj.email},
            )
            admin_msg = EmailMultiAlternatives(
                subject="New Newsletter Subscription",
                body=f"New subscriber: {obj.email}",
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[settings.ADMIN_EMAIL],
            )
            admin_msg.attach_alternative(admin_html, "text/html")
            admin_msg.send(fail_silently=True)
        except Exception:
            pass

        # Always send user thank-you (even if already subscribed)
        try:
            user_html = render_to_string(
                "emails/newsletter_welcome.html",
                {"email": obj.email, "already": not created},
            )
            user_msg = EmailMultiAlternatives(
                subject="Thanks for subscribing to MindMines",
                body="Thanks for subscribing to MindMines!",
                from_email=settings.DEFAULT_FROM_EMAIL,
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


# -------------------- Wishlist --------------------

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

# -------------------- Orders (read/create/invoice) --------------------

class OrderListAPIView(generics.ListAPIView):
    serializer_class = OrderReadSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Order.objects.filter(user=self.request.user).order_by('-created_at')

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        ser = self.get_serializer(queryset, many=True, context={"request": request})
        return Response(ser.data)


class OrderCreateView(APIView):
    permission_classes = [IsAuthenticated]
    def post(self, request):
        ser = OrderCreateSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)
        with transaction.atomic():
          order = ser.save()
        return Response(OrderReadSerializer(order, context={"request": request}).data, status=201)
        

class OrderDetailAPIView(generics.RetrieveAPIView):
    serializer_class = OrderReadSerializer
    permission_classes = [IsAuthenticated]
    def get_queryset(self):
        return Order.objects.filter(user=self.request.user)


class OrderInvoiceAPIView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request, pk):
        try:
            order = Order.objects.get(pk=pk, user=request.user)
        except Order.DoesNotExist:
            raise Http404("Order not found")

        # Ensure invoice exists (utils should create Invoice instance)
        if not hasattr(order, "invoice") or not order.invoice.pdf:
            generate_invoice_pdf(order)  # should attach/return Invoice model

        inv = order.invoice
        return FileResponse(inv.pdf.open("rb"),
                            content_type="application/pdf",
                            as_attachment=True,
                            filename=f"invoice_{inv.number}.pdf")

class OrderAdminUpdateAPIView(APIView):
    permission_classes = [IsAdminUser]

    def patch(self, request, pk):
        order = get_object_or_404(Order, pk=pk)
        allowed = {"status", "tracking_number"}
        updated = False
        prev_status = order.status
        status_in = request.data.get("status")
        tracking_in = request.data.get("tracking_number")

        for field in allowed:
            if field in request.data:
                setattr(order, field, request.data[field])
                updated = True

        if not updated:
            return Response({"detail": "No updatable fields provided."}, status=400)

        order.save()



         # 🔵 If status turned PAID, generate invoice + send emails
        if prev_status != "paid" and order.status == "paid":
            inv = generate_invoice_pdf(order)
            try:
                user_locale = getattr(getattr(order.user, "profile", None), "language", "en") or "en"
                send_invoice_email(order, user_locale=user_locale)
            except Exception:
                pass
            try:
                locale = getattr(getattr(order.user, "profile", None), "language", "en") or "en"
                send_order_confirmation(order, user_locale=locale)
            except Exception:
                pass
        if prev_status != "cancelled" and order.status == "cancelled":
            order.release_stock()

        return Response(OrderReadSerializer(order, context={"request": request}).data, status=200)
    


class SetTrackingAPIView(APIView):
    permission_classes = [IsAdminUser]
    def post(self, request, pk):
        tracking = (request.data.get("tracking_number") or "").strip()
        order = get_object_or_404(Order, pk=pk)
        order.tracking_number = tracking
        order.save(update_fields=["tracking_number"])
        inv = generate_invoice_pdf(order)
        user_locale = getattr(getattr(order.user, "profile", None), "language", "en") or "en"
        send_invoice_email(order, user_locale=user_locale)
        return Response({"status": "ok", "invoice": inv.number})
    

#-----------------------coupon-------------------
class CouponPreviewAPIView(APIView):
    permission_classes = [AllowAny]  # if you require auth, switch
    def post(self, request):
        s = CouponPreviewIn(data=request.data)
        s.is_valid(raise_exception=True)
        code  = s.validated_data["code"]
        items = s.validated_data["items"]

        # compute subtotal (gross)
        subtotal = Decimal("0.00")
        for it in items:
            p = Product.objects.get(pk=it["product_id"])
            subtotal += Product.vat_included_price(p) * int(it["qty"])

        from server.models import Coupon
        coup = Coupon.objects.filter(code__iexact=code.strip()).first()
        if not coup:
            return Response({"ok": False, "error": "Coupon not found"}, status=404)

        ok, reason = coup.can_apply(request.user if request.user.is_authenticated else None, items, subtotal)
        if not ok:
            return Response({"ok": False, "error": reason}, status=400)

        discount = coup.discount_amount(request.user if request.user.is_authenticated else None, items, subtotal)
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

#------------------Promotion-------------
class ActivePromotionAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        # Use Europe/Berlin by default
        now = timezone.now()
        # Optional country targeting (from query or headers)
        country = (request.GET.get("country")
                   or request.headers.get("X-Country")
                   or "DE").upper()

        qs = Promotion.objects.filter(
            is_enabled=True,
            start_at__lte=now,
            end_at__gte=now,
        ).order_by("-priority", "-updated_at")

        # allow empty whitelist or list containing country
        qs = qs.filter(Q(country_whitelist=[]) | Q(country_whitelist__contains=[country]))

        promo = qs.first()
        if not promo:
            # No active promo
            return Response({"active": False}, status=200)

        data = PromotionSerializer(promo, context={"request": request}).data
        data["active"] = True
        return Response(data, status=200)

# -------------------- Misc --------------------

@api_view(["GET"])
def getRoutes(request):
    return Response(["/users/token/", "/users/register/", "/users/token/refresh/"])

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def testEndPoint(request):
    if request.method == "GET":
        data = f"Congratulation {request.user}, your API just responded to GET request"
        return Response({"response": data}, status=status.HTTP_200_OK)
    text = "Hello buddy"
    data = f"Congratulation your API just responded to POST request with text: {text}"
    return Response({"response": data}, status=status.HTTP_200_OK)

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
        html_body = render_to_string("emails/verification_email.html", {
            "user": user, "verification_link": link,
        })
    except TemplateDoesNotExist:
        pass

    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=from_email,
        to=[user.email],
    )
    if html_body:
        msg.attach_alternative(html_body, "text/html")
    msg.send(fail_silently=not settings.DEBUG)

    payload = {"detail": "Verification email sent."}
    if settings.DEBUG:
        payload["dev_verification_link"] = link
    return Response(payload, status=200)

# -------------------- Helpers for payment lines --------------------

def _order_lines(items):
    lines = []
    total_tax = 0
    for it in items:
        p = Product.objects.get(pk=it["product_id"])
        qty = int(it["qty"])
        unit = Decimal(p.final_price)   
        total = unit * qty
        net = (total / Decimal("1.19"))
        tax = total - net
        lines.append({
            "name": p.title_en[:255],
            "quantity": qty,
            "unit_price": int(unit * 100),
            "total_amount": int(total * 100),
            "tax_rate": 1900,
            "total_tax_amount": int(tax * 100),
        })
        total_tax += tax
    return lines, int(total_tax * 100)

def _compute_from_items(items):
    subtotal = Decimal("0.00")
    for it in items:
        p = Product.objects.get(pk=it["product_id"])
        subtotal += (Decimal(p.final_price) * int(it["qty"]))  
    shipping = Decimal("0.00")
    total = (subtotal + shipping).quantize(Decimal("0.01"))
    return subtotal, shipping, total

# -------------------- PayPal --------------------

def d2(x):
    return Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

class PaypalBase(APIView):
    permission_classes = [IsAuthenticated]

    def _creds_ok(self):
        return bool(settings.PAYPAL_CLIENT_ID and settings.PAYPAL_CLIENT_SECRET)

    def _base(self):
        if getattr(settings, "PAYPAL_ENV", "live") == "live":
            return "https://api-m.paypal.com"
        return "https://api-m.sandbox.paypal.com"

    def _token(self):
        url = f"{self._base()}/v1/oauth2/token"
        try:
            r = http.post(
                url,
                data={"grant_type": "client_credentials"},
                auth=(settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET),
                timeout=30
            )
        except Exception as e:
            return None, {"error": "token_request_failed", "detail": str(e)}
        if r.status_code != 200:
            body = {}
            try:
                body = r.json()
            except Exception:
                pass
            return None, {"error": "token_http_error", "status": r.status_code, "body":body}
        data = r.json()
        return data.get("access_token"), None


class PaypalCreateOrder(PaypalBase):
    permission_classes = [AllowAny] 

    def post(self, request):
        if not self._creds_ok():
            return Response({"detail": "PayPal credentials not configured."}, status=400)

        # create local order from cart/payload
        try:
            s = OrderCreateSerializer(data=request.data, context={"request": request})
            s.is_valid(raise_exception=True)
            with transaction.atomic():
                order = s.save()
                order.payment_method = "paypal"
                order.save(update_fields=["payment_method"])
        except Exception as e:
            return Response({"detail": "LOCAL_ORDER_CREATE_FAILED", "error": str(e)}, status=500)

        # get PayPal token
        token, err = self._token()
        if not token:
            return Response({"detail": "FAILED_TO_GET_TOKEN", "error": err}, status=502)

        # create PayPal order
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

        try:
            resp = http.post(
                f"{self._base()}/v2/checkout/orders",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                data=json.dumps(payload),
                timeout=30
            )
            data = resp.json()
        except Exception as e:
            return Response({"detail": "PAYPAL_CREATE_HTTP_FAILED", "error": str(e)}, status=502)

        if resp.status_code not in (200, 201):
            return Response({"detail": "PAYPAL_CREATE_ERROR", "paypal": data}, status=502)

        try:
            order.payment_ref = data.get("id", "")
            order.save(update_fields=["payment_ref"])
        except Exception as e:
            return Response({"detail": "LOCAL_UPDATE_FAILED", "error": str(e)}, status=500)

        return Response({"id": data.get("id"), "order_id": order.id}, status=201)

class PaypalCaptureOrder(PaypalBase):
    permission_classes = [AllowAny]

    def post(self, request, order_id):
        if not self._creds_ok():
            return Response({"detail": "PayPal credentials not configured."}, status=400)

        # Optional: stricter linkage
        local_order_id = request.data.get("local_order_id")

        # Find the local order expecting this PayPal order
        try:
            q = {"payment_ref": order_id}
            if local_order_id:
                q["id"] = local_order_id
            ord_obj = Order.objects.select_for_update().get(**q)
        except Order.DoesNotExist:
            return Response({"detail": "Local order not found for this PayPal order."}, status=404)

        if ord_obj.status == "paid":
            return Response({"status": "COMPLETED"}, status=200)

        token, err = self._token()
        if not token:
            return Response({"detail": "FAILED_TO_GET_TOKEN", "error": err}, status=502)

        cap_url = f"{self._base()}/v2/checkout/orders/{order_id}/capture"
        try:
            resp = http.post(
                cap_url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                timeout=30,
            )
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text}
        except Exception as e:
            return Response({"detail": "PAYPAL_CAPTURE_HTTP_FAILED", "error": str(e)}, status=502)

        if resp.status_code not in (200, 201):
            return Response({"detail": "PAYPAL_CAPTURE_ERROR", "paypal": data}, status=502)

        if data.get("status") != "COMPLETED":
            return Response({"detail": "CAPTURE_NOT_COMPLETED", "paypal": data}, status=400)

        # amount/ccy verification
        try:
            from decimal import Decimal, ROUND_HALF_UP
            pu = (data.get("purchase_units") or [{}])[0]
            cap = (pu.get("payments", {}).get("captures") or [{}])[0]
            cap_amt = cap.get("amount", {}) or {}
            cap_ccy = cap_amt.get("currency_code")
            cap_val = Decimal(cap_amt.get("value", "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            local_total = Decimal(ord_obj.total_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except Exception:
            return Response({"detail": "CAPTURE_PARSE_ERROR", "paypal": data}, status=400)

        if cap_ccy != "EUR" or cap_val != local_total:
            return Response(
                {"detail": "CAPTURE_AMOUNT_MISMATCH", "paypal": cap_amt, "local": f"{local_total:.2f}"},
                status=400,
            )

        # Mark paid + side effects
        if ord_obj.status != "paid":
            ord_obj.status = "paid"
            ord_obj.save(update_fields=["status"])
            _safe(generate_invoice_pdf, ord_obj)
            _safe(send_invoice_email, ord_obj)
            _safe(fire_and_forget, post_payment_pipeline, ord_obj.id)
            locale = getattr(getattr(ord_obj.user, "profile", None), "language", "en") or "en"
            _safe(send_order_confirmation, ord_obj, user_locale=locale)

        return Response({"status": "COMPLETED"}, status=200)
    
# ------------ Stripe ---------------------------
import stripe
try:
    stripe_error = stripe.error     
except AttributeError:
    from stripe import _error as stripe_error 

stripe.api_key = settings.STRIPE_SECRET_KEY

PAYMENT_METHOD_MAP = {
    "card": "card",
    "sepa_debit": "sepa_debit"
}

from typing import List, Optional
from typing import List

def _split_installments_eur(total_cents: int, months: int) -> List[Decimal]:
    """
    Return a list of Decimal EUR amounts (2dp) for each month.
    Last month absorbs remainder; matches your schedule math.
    """
    base = total_cents // months
    rem = total_cents - base * months
    cents = [base] * months
    if rem:
        cents[-1] += rem
    return [
        (Decimal(c) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        for c in cents
    ]


def _month_dates(anchor_dt, months: int):
    """Return list of datetimes (tz-aware) month-by-month from anchor_dt."""
    return [anchor_dt + relativedelta(months=i) for i in range(months)]


def _amount_cents(d: Decimal) -> int:
    """Convert Decimal amount in EUR to integer cents."""
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
def _get_or_create_stripe_customer(user=None, email: Optional[str] = None) -> str:
        if user is not None and getattr(user, "is_authenticated", False):
            prof = getattr(user, "profile", None)
            # reuse if already known
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
    
        # Guest path (no user)
        customer = stripe.Customer.create(
            email=email or None,
            metadata={"guest": "true"},
        )
        return customer.id

def _extract_email_from_payload(data: dict) -> Optional[str]:
    """
    Try a few common places you may be sending an email in your order payload.
    Adjust to match your serializer/payload.
    """
    return (
        data.get("email")
        or (data.get("billing") or {}).get("email")
        or (data.get("shipping_address") or {}).get("email")
        or None
    )
  


# ============================================================================
# 1) Unified one-off PaymentIntent (card, sepa_debit, giropay, sofort)
# ============================================================================
# WARNING: Only use CsrfExemptSessionAuthentication on endpoints where CSRF protection is intentionally disabled and it is safe to do so.
class CsrfExemptSessionAuthentication(SessionAuthentication):
    def enforce_csrf(self, request):
        # Skip CSRF checks for this endpoint (guest checkout from SPA)
        return
    
class StripeCreateIntent(APIView):
    """
    Creates a PaymentIntent for one-off payments.
    Body expects your existing order payload with:
      payment_method ∈ {"card","sepa_debit","giropay","sofort"}
    Client must confirm with Stripe.js (Payment Element, confirmCardPayment,
    confirmSepaDebitPayment, etc).
    """
    permission_classes = [AllowAny]
    authentication_classes = [CsrfExemptSessionAuthentication, BasicAuthentication]
    
    def post(self, request):
        data = request.data.copy()
        pm = (data.get("payment_method") or "card").strip().lower()
        pm_type = PAYMENT_METHOD_MAP.get(pm)
        if not pm_type:
            return Response({"detail": f"Unsupported payment_method '{pm}'"}, status=400)

        # Build order in 'pending'
        
        s = OrderCreateSerializer(data=data, context={"request": request})
        s.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                order = s.save()
                order.payment_method = pm
                order.status = "pending"
                order.save(update_fields=["payment_method", "status"])
        except serializers.ValidationError as ve:
            return Response({"detail": "Order validation failed", "errors": ve.detail}, status=400)
        except Exception as e:
            # helpful during debugging
            return Response({"detail": "Order creation failed", "error": str(e)}, status=400)

        # Ensure Stripe Customer (guest-safe)
        try:
            if request.user.is_authenticated:
                customer_id = _get_or_create_stripe_customer(request.user)
            else:
                # guest: derive from order.guest_email / name (set by serializer)
                guest_email = order.guest_email or (data.get("email") or None)
                guest_name  = order.guest_name  or (data.get("name") or None)
                if not guest_email:
                    return Response({"detail": "Email is required for guest checkout."}, status=400)
                # You can try to re-use by email; simplest is to always create
                cust = stripe.Customer.create(
                    email=guest_email,
                    name=guest_name or None,
                    metadata={"guest_order_id": str(order.id)},
                )
                customer_id = cust.id

        except stripe_error.StripeError as e:
            order.status = "failed"
            order.save(update_fields=["status"])
            return Response({"detail": f"Stripe customer error: {str(e)}"}, status=502)

        # Optional: tweak options for specific PMs
        payment_method_options = {}
        if pm_type == "sofort":
            lang = (request.META.get("HTTP_ACCEPT_LANGUAGE", "en").split(",")[0] or "en")[:2]
            payment_method_options["sofort"] = {"preferred_language": lang}

        try:
            intent = stripe.PaymentIntent.create(
                amount=_amount_cents(order.total_price),
                currency=getattr(settings, "STRIPE_CURRENCY", "eur"),
                payment_method_types=[pm_type],
                metadata={"order_id": str(order.id)},
                customer=customer_id,
                setup_future_usage="off_session" if pm_type == "card" else None,
                description=f"Order {order.number or order.id} — MindMines",
                payment_method_options=payment_method_options or None,
                idempotency_key=f"pi-create-{order.id}",
            )
        except stripe_error.StripeError as e:
            order.status = "failed"
            order.save(update_fields=["status"])
            order.release_stock()
            return Response({"detail": f"Stripe error: {str(e)}"}, status=502)
        except Exception as e:
            order.status = "failed"
            order.save(update_fields=["status"])
            order.release_stock()
            return Response({"detail": f"Server error: {str(e)}"}, status=500)

        order.payment_ref = intent.id
        order.save(update_fields=["payment_ref"])

        # (Optional) SEPA pending email
        if pm_type == "sepa_debit":
            try:
                locale = (
                    getattr(getattr(request, "user", None), "profile", None) and
                    getattr(request.user.profile, "language", None)
                ) or (request.META.get("HTTP_ACCEPT_LANGUAGE", "en").split(",")[0] or "en")
                send_sepa_pending_email(order, user_locale=locale)
            except Exception:
                pass

        return Response({"clientSecret": intent.client_secret, "order_id": order.id}, status=201)


# ============================================================================
# 1b) Save card for later (authenticated)
# ============================================================================
class StripeCreateCardSetupIntent(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        try:
            customer_id = _get_or_create_stripe_customer(request.user)
            si = stripe.SetupIntent.create(
                customer=customer_id,
                payment_method_types=["card"],
                usage="off_session",
            )
            return Response({"clientSecret": si.client_secret})
        except stripe_error.CardError as e:
            return Response({"detail": str(e)}, status=400)
        except stripe_error.StripeError as e:
            return Response({"detail": str(e)}, status=502)
        except Exception as e:
            return Response({"detail": str(e)}, status=500)


# ============================================================================
# 2) Save SEPA mandate for installments (authenticated)
# ============================================================================
class StripeCreateSepaSetupIntent(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            customer_id = _get_or_create_stripe_customer(request.user)
            si = stripe.SetupIntent.create(
                customer=customer_id,
                payment_method_types=["sepa_debit"],
                usage="off_session",
            )
            return Response({"clientSecret": si.client_secret})
        except stripe_error.CardError as e:
            return Response({"detail": str(e)}, status=400)
        except stripe_error.StripeError as e:
            return Response({"detail": str(e)}, status=502)
        except Exception as e:
            return Response({"detail": str(e)}, status=500)


# ============================================================================
# 3) Legacy SEPA one-off (keep; now guest-friendly too)
# ============================================================================
class StripeCreateSepaIntent(APIView):
    permission_classes = [AllowAny]
    authentication_classes = [CsrfExemptSessionAuthentication, BasicAuthentication]

    def post(self, request):
        s = OrderCreateSerializer(data=request.data, context={"request": request})
        s.is_valid(raise_exception=True)

        with transaction.atomic():
            order = s.save()
            order.payment_method = "sepa_debit"
            order.status = "pending"
            order.save(update_fields=["payment_method", "status"])

        try:
            email = _extract_email_from_payload(request.data)
            user = request.user if getattr(request, "user", None) and request.user.is_authenticated else None
            customer_id = _get_or_create_stripe_customer(user=user, email=email)
            intent = stripe.PaymentIntent.create(
                amount=_amount_cents(order.total_price),
                currency=getattr(settings, "STRIPE_CURRENCY", "eur"),
                payment_method_types=["sepa_debit"],
                description=f"Order {order.number or order.id} — MindMines",
                metadata={"order_id": str(order.id)},
                customer=customer_id,
                confirm=False,
                idempotency_key=f"pi-create-{order.id}",
            )
        except stripe_error.CardError as e:
            order.status = "failed"
            order.save(update_fields=["status"])
            return Response({"detail": str(e)}, status=400)
        except stripe_error.StripeError as e:
            order.status = "failed"
            order.save(update_fields=["status"])
            return Response({"detail": str(e)}, status=502)
        except Exception as e:
            order.status = "failed"
            order.save(update_fields=["status"])
            return Response({"detail": str(e)}, status=500)

        order.payment_ref = intent.id
        order.save(update_fields=["payment_ref"])

        try:
            from server.utils.email import send_sepa_pending_email
            locale = (
                getattr(getattr(order, "user", None), "profile", None) and
                getattr(order.user.profile, "language", None)
            ) or "en"
            send_sepa_pending_email(order, user_locale=locale)
        except Exception:
            pass

        return Response({"clientSecret": intent.client_secret, "order_id": order.id}, status=201)


# ============================================================================
# 4) Installments (authenticated)
# ============================================================================
class StripeSepaCreateInstallments(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        months = int(request.data.get("months") or 0)
        if months not in (3, 6, 9):
            return Response({"detail": "Invalid months. Choose 3, 6, or 9."}, status=400)
   
        country = (request.data.get("country") or "").upper()[:2]
        if country != "DE":
            return Response({"detail": "Installments are available in DE only."}, status=400)
        method = (request.data.get("method") or "sepa_debit").strip()
        if method == "sepa_debit" and country != "DE":
            return Response({"detail": "SEPA installments are available in DE only."}, status=400)
        
        s = OrderCreateSerializer(data=request.data, context={"request": request})
        s.is_valid(raise_exception=True)
        with transaction.atomic():
            order = s.save()
            order.payment_method = method
            order.installments_months = months
            order.status = "pending"
            order.save(update_fields=["payment_method", "installments_months", "status"])

        total_cents = _amount_cents(order.total_price)
        base = total_cents // months
        remainder = total_cents - base * months

        pm_id = request.data.get("payment_method_id")
        if not pm_id:
            return Response({"detail": "payment_method_id is required"}, status=400)

        try:
            # Ensure the schedule uses the SAME customer as the PM
            pm = stripe.PaymentMethod.retrieve(pm_id)
            pm_customer = pm.get("customer")
            if pm_customer:
                customer_id = pm_customer
                prof = getattr(request.user, "profile", None)
                if prof and not getattr(prof, "stripe_customer_id", ""):
                    prof.stripe_customer_id = customer_id
                    prof.save(update_fields=["stripe_customer_id"])
            else:
                customer_id = _get_or_create_stripe_customer(request.user)
                stripe.PaymentMethod.attach(pm_id, customer=customer_id)

            stripe.Customer.modify(customer_id, invoice_settings={"default_payment_method": pm_id})

            def mk_price(amount: int, label: str):
                return stripe.Price.create(
                    unit_amount=amount,
                    currency=getattr(settings, "STRIPE_CURRENCY", "eur"),
                    recurring={"interval": "month"},
                    product_data={"name": label},
                )

            now = timezone.now()
            def ts(dt): return int(dt.timestamp())

            phases = []
            title_base = f"Installments for Order {order.number or order.id}"

            if remainder:
                if months > 1:
                    price_base = mk_price(base, f"{title_base} (monthly)")
                    end1 = now + relativedelta(months=months - 1)
                    phases.append({"items": [{"price": price_base.id, "quantity": 1}], "end_date": ts(end1)})
                price_last = mk_price(base + remainder, f"{title_base} (final month adj)")
                end2 = now + relativedelta(months=months)
                phases.append({"items": [{"price": price_last.id, "quantity": 1}], "end_date": ts(end2)})
            else:
                price_all = mk_price(base, f"{title_base} (monthly)")
                end_all = now + relativedelta(months=months)
                phases.append({"items": [{"price": price_all.id, "quantity": 1}], "end_date": ts(end_all)})

            schedule = stripe.SubscriptionSchedule.create(
                customer=customer_id,
                start_date="now",
                end_behavior="cancel",
                phases=phases,
                default_settings={
                    "default_payment_method": pm_id,
                    "collection_method": "charge_automatically",
                },
                metadata={"order_id": str(order.id)},
            )

            order.payment_ref = schedule.id
            order.status = "processing"
            order.save(update_fields=["payment_ref", "status"])

            # Email plan details (optional)
            try:
                amounts_eur = _split_installments_eur(total_cents, months)
                anchor_dt = timezone.now()
                try:
                    sch = stripe.SubscriptionSchedule.retrieve(schedule.id)
                    sub = sch.get("subscription")
                    anchor_ts = sub.get("billing_cycle_anchor") if sub else None
                    if not anchor_ts:
                        phases0 = (sch.get("phases") or [{}])[0]
                        anchor_ts = phases0.get("start_date") or sch.get("start_date")
                    if anchor_ts:
                        anchor_dt = datetime.fromtimestamp(anchor_ts, tz=timezone.utc)
                except Exception:
                    pass
                dates = _month_dates(anchor_dt, months)
                user_locale = getattr(getattr(order.user, "profile", None), "language", "en") or "en"
                send_installments_started_email(order, months, amounts_eur, dates, user_locale=user_locale)
            except Exception:
                import logging; logging.getLogger(__name__).exception("installments started email failed")

            return Response({"order_id": order.id, "schedule_id": schedule.id}, status=201)

        except stripe_error.CardError as e:
            return Response({"detail": e.user_message or str(e)}, status=400)
        except stripe_error.StripeError as e:
            return Response({"detail": str(e)}, status=502)
        except Exception as e:
            return Response({"detail": str(e)}, status=500)


# ============================================================================
# 6) Webhook: mark orders paid/failed
# ============================================================================
@csrf_exempt
def stripe_webhook(request):
    import logging
    log = logging.getLogger(__name__)

    wh_secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", "")
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
    if not sig_header:
        return JsonResponse({"detail": "Missing Stripe-Signature header"}, status=400)

    payload = request.body  # raw bytes

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, wh_secret)
    except stripe_error.SignatureVerificationError:
        return JsonResponse({"detail": "Invalid webhook signature"}, status=400)
    except stripe_error.StripeError as e:
        return JsonResponse({"detail": f"Stripe error: {str(e)}"}, status=400)
    except Exception as e:
        return JsonResponse({"detail": f"Bad payload: {str(e)}"}, status=400)

    etype = event.get("type")
    data = event["data"]["object"]

    log.info("WEBHOOK %s object=%s", etype, data.get("id"))

    def _mark_processing(order: Order):
        if order.status != "processing":
            order.status = "processing"
            order.save(update_fields=["status"])

    def _mark_paid(order: Order):
        changed = False
        if order.status != "paid":
            order.status = "paid"
            changed = True

        if not getattr(order, "payment_id", ""):
            ch_id = (data.get("charges", {}).get("data") or [{}])[0].get("id") or data.get("id","")
            if ch_id:
                order.payment_id = ch_id
                changed = True

        if changed:
            order.save(update_fields=["status", "payment_id"])

        order.assign_unique_invoice_number()
        generate_invoice_pdf(order)

        user_locale = getattr(getattr(order.user, "profile", None), "language", "en") or "en"
        log.info("WEBHOOK emailing invoice for order %s", order.id)
        try:
            send_invoice_email(order, user_locale=user_locale)
        except Exception:
            log.exception("send_invoice_email failed for order %s", order.id)

    if etype == "payment_intent.processing":
        oid = (data.get("metadata") or {}).get("order_id")
        if oid:
            try:
                order = Order.objects.get(pk=oid)
                _mark_processing(order)
                user_locale = getattr(getattr(order.user, "profile", None), "language", "en") or "en"
                try:
                    send_sepa_pending_email(order, user_locale=user_locale)
                except Exception:
                    log.exception("send_sepa_pending_email failed for order %s", order.id)
            except Order.DoesNotExist:
                pass

    elif etype == "payment_intent.succeeded":
        oid = (data.get("metadata") or {}).get("order_id")
        if oid:
            try:
                order = Order.objects.select_for_update().get(pk=oid)
                try:
                    order.reserve_stock()
                except Exception as e:
                    log.exception("reserve_stock failed for order %s:%s", order.id, e)
                _mark_paid(order)
            except Order.DoesNotExist:
                pass

    elif etype == "invoice.payment_succeeded":
        oid = (data.get("metadata") or {}).get("order_id")
        if oid:
            try:
                order = Order.objects.get(pk=oid)
                _mark_paid(order)
            except Order.DoesNotExist:
                pass

    elif etype in ("payment_intent.payment_failed", "invoice.payment_failed"):
        oid = (data.get("metadata") or {}).get("order_id")
        if oid:
            try:
                order = Order.objects.select_for_update().get(pk=oid)
                # mark failed
                order.status = "failed"
                order.save(update_fields=["status"])
                # ✅ release reserved stock if not already released
                order.release_stock()
            except Order.DoesNotExist:
                pass
    return HttpResponse(status=200)


# -------------------- Bank transfer --------------------

class CreateBankTransferOrder(APIView):
   
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data.copy()
        data.pop("payment_method", None)
        s = OrderCreateSerializer(data=data, context={"request": request})
        s.is_valid(raise_exception=True)
        with transaction.atomic():
            order = s.save()
            order.payment_method = "bank_transfer"
            order.status = "pending"  
            order.save(update_fields=["payment_method", "status"])

        # ✅ email the customer bank instructions + order summary
        try:
            locale = getattr(getattr(order.user, "profile", None), "language", "en") or "en"
            send_bank_transfer_instructions(
                order,
                {
                    "iban": settings.BANK_TRANSFER["IBAN"],
                    "bic": settings.BANK_TRANSFER["BIC"],
                    "account_name": settings.BANK_TRANSFER["ACCOUNT_NAME"],
                },
                user_locale=locale,
                due_hours=settings.BANK_TRANSFER["PAYMENT_DUE_HOURS"],
            )
        except Exception:
            import logging; logging.getLogger(__name__).exception("Bank transfer email failed")

        return self._resp(order)

    def _resp(self, order):
        return Response(
            {
                "ok": True,
                "order_id": order.id,
                "order_number": order.number,
                "amount": f"{order.total_price:.2f}",
                "currency": "EUR",
                "iban": settings.BANK_TRANSFER["IBAN"].replace(" ", ""),
                "bic": settings.BANK_TRANSFER["BIC"],
                "beneficiary": settings.BANK_TRANSFER["ACCOUNT_NAME"],
                "reference": order.number,  # use exactly this in the transfer
                "instructions": f"Please transfer the total amount using the reference above within {settings.BANK_TRANSFER['PAYMENT_DUE_HOURS']} hours.",
            },
            status=201,
        )

# ✅ Invoice download (user)
class InvoiceDownloadView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request, pk):
        order = get_object_or_404(Order, pk=pk, user=request.user)
        if not hasattr(order, "invoice") or not order.invoice.pdf:
            return Response({"detail": "Invoice not available."}, status=404)
        return FileResponse(order.invoice.pdf.open("rb"), as_attachment=True, filename=f"{order.invoice.number}.pdf")
    
# -------------------- Returns / Cancellations --------------------

def _can_return(order: Order) -> tuple[bool, str]: 
    """Return only within RETURNS_WINDOW_DAYS after delivery.""" 
    if order.status != "delivered": 
        return False, "Returns are only possible after delivery." 
    if not order.delivered_at: 
        return False, "Delivery timestamp missing for this order." 
    
    now = timezone.now() 
    age_days = (now - order.delivered_at).days 
    if age_days > RETURNS_WINDOW_DAYS: 
        return False, f"Return window ({RETURNS_WINDOW_DAYS} days) has expired." 
    return True, None
    return True, None


RETURNS_WINDOW_DAYS = getattr(settings, "RETURNS_WINDOW_DAYS", 1)

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

        rr = ReturnRequest.objects.create(
            order=order,
            user=request.user,
            request_type="return",
            message=message,
        )

        # Save attachments (images/videos)
        for f in request.FILES.getlist("files"):
            ReturnAttachment.objects.create(request=rr, file=f)

        # Send emails — SINGLE ARGUMENT
        email_err = None
        try:
            send_return_request_emails(rr)
            email_sent = True
        except Exception as e:
            # Don’t break the request if email fails; report it in JSON for debugging
            email_sent = False
            email_err = f"{e.__class__.__name__}: {e}"

        return Response(
            {"ok": True, "request_id": rr.id, "email_sent": email_sent, "error": email_err},
            status=status.HTTP_201_CREATED,
        )


    
class OrderCancelRequestAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk: int):
        order = get_object_or_404(Order, pk=pk, user=request.user)

        # business rules: cancellable if pending/processing/shipped
        # or delivered within 15 days
        allowed = False
        if order.status in ("pending", "processing", "shipped"):
            allowed = True
        elif order.status == "delivered" and order.delivered_at:
            CANCELLATION_WINDOW_DAYS = getattr(settings, "CANCELLATION_WINDOW_DAYS", RETURNS_WINDOW_DAYS)
            delta = timezone.now() - order.delivered_at
            allowed = (delta.days <= CANCELLATION_WINDOW_DAYS)

        if not allowed:
            return Response(
                {"detail": "This order can no longer be cancelled."},
                status=400,
            )

        msg = (request.data.get("message") or "").strip()
        rr = ReturnRequest.objects.create(
            order=order,
            user=request.user,
            request_type="cancel",
            message=msg,
        )

        # if you decide to allow files on cancel:
        for f in request.FILES.getlist("files"):
            ReturnAttachment.objects.create(request=rr, file=f)

        # send emails
        try:
            send_cancel_request_emails(rr)
        except Exception:
            logger.exception("Failed to send cancel emails for rr=%s", rr.pk)

        return Response(
            {
                "id": rr.pk,
                "status": rr.status,
                "request_type": rr.request_type,
            },
            status=201,
        )
# --- GUEST CHECKOUT LOGIN ---
from uuid import uuid4

def _get_or_create_guest_user(email: str, full_name: str = ""):
    """
    Create or reuse a 'guest' user. Keep is_active=False so it doesn't act as a normal account.
    Assign an unusable password.
    """
    email = (email or "").strip().lower()
    User = get_user_model()

    user = User.objects.filter(email=email).first()
    if user:
        # If it already exists but is a "guest" (inactive), reuse it
        if not user.is_active:
            # update name if newly provided
            if full_name and hasattr(user, "profile"):
                user.profile.full_name = full_name
                user.profile.save(update_fields=["full_name"])
            return user
        # Active user with same email is handled earlier in serializer validation
        return user

    # Create a fresh guest account
    username = f"guest-{uuid4().hex[:12]}"
    user = User.objects.create(
        email=email,
        username=username,
        is_active=False,  # mark as guest/unverified
    )
    user.set_unusable_password()
    user.save()

    # optional: tag profile
    if hasattr(user, "profile"):
        if full_name:
            user.profile.full_name = full_name
        # You could add a boolean field like `is_guest` in Profile if desired
        user.profile.save()

    return user


class GuestCheckoutLoginView(APIView):
    """
    Issue a short-lived JWT for a 'guest' user so existing authenticated-only
    order/payment endpoints can be reused without schema changes.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        from .serializers import GuestCheckoutIn
        ser = GuestCheckoutIn(data=request.data)
        ser.is_valid(raise_exception=True)

        email = ser.validated_data["email"]
        full_name = ser.validated_data.get("full_name", "")

        # If a real account exists with this email, the serializer already blocked it.
        user = _get_or_create_guest_user(email, full_name)

        # Mint tokens reusing your existing JWT serializer
        refresh = RefreshToken.for_user(user)
        return Response({
            "refresh": str(refresh),
            "access": str(refresh.access_token),
            "user": {"id": user.id, "email": user.email, "username": user.username, "guest": True},
        }, status=200)

class GuestLookupThrottle(throttling.SimpleRateThrottle):
    scope = "guest_lookup"

    def get_cache_key(self, request, view):
        # throttle by IP for POSTs
        ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}

class GuestOrderLookupView(APIView):
    """
    POST { email, number } -> public order details if email matches the order's customer email.
    """
    permission_classes = [permissions.AllowAny]
    throttle_classes = [GuestLookupThrottle]

    def post(self, request):
        inp = GuestOrderLookupInputSerializer(data=request.data)
        inp.is_valid(raise_exception=True)
        email = inp.validated_data["email"].strip()
        number = inp.validated_data["number"].strip()

        # IMPORTANT:
        # - number should be unique (your order.number)
        # - email is compared case-insensitively
        qs = Order.objects.select_related("shipping_address").prefetch_related("items__product")
        order = qs.filter(number=number).first()
        if not order:
            return Response({"detail": "Order not found."}, status=status.HTTP_404_NOT_FOUND)

        order_email = (getattr(order, "email", "") or getattr(order, "guest_email", "") or "").strip()
        if order_email.lower() != email.lower():
            return Response({"detail": "Order not found."}, status=status.HTTP_404_NOT_FOUND)
        ser = OrderPublicSerializer(order, context={"request": request})
        return Response(ser.data, status=status.HTTP_200_OK)
