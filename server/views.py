
from decimal import Decimal,InvalidOperation
import json
import requests as http

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail, EmailMultiAlternatives
from django.http import FileResponse , Http404
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string,TemplateDoesNotExist
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.db import transaction
from django.utils import timezone
from datetime import timedelta
from django.db.models import Avg, Count



from google.oauth2 import id_token
from google.auth.transport.requests import Request as GoogleRequest

from rest_framework import generics, status, filters
from rest_framework.decorators import api_view, permission_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import AllowAny, IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework import permissions
import re

from .pagination import StandardResultsSetPagination
from .serializers import (
    ProfileSerializer, RegisterSerializer, MyTokenObtainPairSerializer,
    ProductSerializer, ContactMessageSerializer,ReviewWriteSerializer,
    NewsletterSubscriberSerializer, AddressSerializer, 
    ReviewSerializer, WishlistSerializer, NaturalStoneSerializer,
    StoneEnquirySerializer, AccountSerializer, OrderCreateSerializer, OrderReadSerializer, 
)
from .models import (
    User, Product, ContactMessage, NewsletterSubscriber,
    Address, Order, Review, Wishlist, NaturalStone, NaturalStoneImage,ReturnRequest,ReturnAttachment
)
from .utils.invoices import generate_invoice_pdf
from .utils.background import fire_and_forget
from .tasks import post_payment_pipeline
from .utils.emails import send_return_request_emails,send_cancel_request_emails,send_order_confirmation, send_bank_transfer_instructions, send_invoice_email

import logging
logger = logging.getLogger(__name__)

User = get_user_model()

# -------------------- Auth / Profile --------------------

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
        try:
            idinfo = id_token.verify_oauth2_token(
                token, GoogleRequest(), settings.SOCIAL_AUTH_GOOGLE_OAUTH2_KEY
            )
            email = idinfo["email"]
            username = email.split("@")[0]
            full_name = idinfo.get("name", "")
            user, created = User.objects.get_or_create(email=email, defaults={
                "username": username, "is_active": True,
            })
            if created and hasattr(user, "profile"):
                user.profile.full_name = full_name
                user.profile.save()
            refresh = RefreshToken.for_user(user)
            return Response({
                "refresh": str(refresh),
                "access": str(refresh.access_token),
                "user": {"id": user.id, "email": user.email, "username": user.username},
            })
        except Exception:
            return Response({"detail": "Google token invalid"}, status=400)

class FacebookLoginAPIView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        access_token = request.data.get("token")
        if not access_token:
            return Response({"detail": "Access token is required"}, status=400)
        debug_url = (
            "https://graph.facebook.com/debug_token"
            f"?input_token={access_token}"
            f"&access_token={settings.FACEBOOK_APP_ID}|{settings.FACEBOOK_APP_SECRET}"
        )
        debug_response = http.get(debug_url).json()
        if not debug_response.get("data", {}).get("is_valid"):
            return Response({"detail": "Invalid Facebook token"}, status=400)
        profile_url = f"https://graph.facebook.com/me?fields=id,name,email&access_token={access_token}"
        fb_data = http.get(profile_url).json()
        email = fb_data.get("email")
        if not email:
            return Response({"detail": "Facebook account must have an email"}, status=400)
        username = email.split("@")[0]
        full_name = fb_data.get("name", "")
        user, created = User.objects.get_or_create(email=email, defaults={
            "username": username, "is_active": True,
        })
        if created and hasattr(user, "profile"):
            user.profile.full_name = full_name
            user.profile.save()
        refresh = RefreshToken.for_user(user)
        return Response({
            "refresh": str(refresh),
            "access": str(refresh.access_token),
            "user": {"id": user.id, "email": user.email, "username": user.username},
        })

# -------------------- Password reset --------------------

class CustomPasswordResetView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    def post(self, request):
        email = request.data.get("email")
        user = User.objects.filter(email=email).first()
        if user:
            uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            reset_url = f"http://localhost:3000/reset-password/{uidb64}/{token}/"
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
            # if you have CartItem:
            # CartItem.objects.filter(user=request.user).delete()
        return Response(OrderReadSerializer(order, context={"request": request}).data, status=201)
        # if ser.is_valid():
        #     order = ser.save()
        #     return Response(OrderReadSerializer(order, context={"request": request}).data, status=201)
        # return Response(ser.errors, status=400)

class OrderDetailAPIView(generics.RetrieveAPIView):
    serializer_class = OrderReadSerializer
    permission_classes = [IsAuthenticated]
    def get_queryset(self):
        return Order.objects.filter(user=self.request.user)
    print("OrderDetailAPIView loaded")

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
                send_invoice_email(order)
            except Exception:
                pass
            try:
                locale = getattr(getattr(order.user, "profile", None), "language", "en") or "en"
                send_order_confirmation(order, user_locale=locale)
            except Exception:
                pass

        return Response(OrderReadSerializer(order, context={"request": request}).data, status=200)
    


class SetTrackingAPIView(APIView):
    permission_classes = [IsAdminUser]
    def post(self, request, pk):
        tracking = (request.data.get("tracking_number") or "").strip()
        order = get_object_or_404(Order, pk=pk)
        order.tracking_number = tracking
        order.save(update_fields=["tracking_number"])
        inv = generate_invoice_pdf(order)
        send_invoice_email(order)
        return Response({"status": "ok", "invoice": inv.number})

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
    base = getattr(settings, "FRONTEND_URL", "http://localhost:3000").rstrip("/")
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
    """Convert items to Klarna order_lines with 19% VAT included in price."""
    lines = []
    total_tax = 0
    for it in items:
        p = Product.objects.get(pk=it["product_id"])
        qty = int(it["qty"])
        unit = Decimal(p.price)     # gross
        total = unit * qty          # gross
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
        print(f"Item {p.id}: unit {unit} total {total} net {net} tax {tax}")
    return lines, int(total_tax * 100)

def _compute_from_items(items):
    subtotal = Decimal("0.00")
    for it in items:
        p = Product.objects.get(pk=it["product_id"])
        subtotal += (Decimal(p.price) * int(it["qty"]))
    shipping = Decimal("0.00") 
    total = (subtotal + shipping).quantize(Decimal("0.01"))
    return subtotal, shipping, total

# -------------------- PayPal --------------------

class PaypalCreateOrder(APIView):
    permission_classes = [IsAuthenticated]

    def _token(self):
        url = "https://api-m.sandbox.paypal.com/v1/oauth2/token" if settings.PAYPAL_ENV == "sandbox" else "https://api-m.paypal.com/v1/oauth2/token"
        r = http.post(url, data={"grant_type": "client_credentials"},
                      auth=(settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET))
        return r.json().get("access_token")

    def post(self, request):
        # build order from items + address
        s = OrderCreateSerializer(data=request.data, context={"request": request})
        s.is_valid(raise_exception=True)
        order = s.save()
        order.payment_method = "paypal"
        order.save(update_fields=["payment_method"])

        token = self._token()
        orders_url = "https://api-m.sandbox.paypal.com/v2/checkout/orders" if settings.PAYPAL_ENV == "sandbox" else "https://api-m.paypal.com/v2/checkout/orders"
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {"currency_code": "EUR", "value": f"{order.total_price:.2f}"}
            }],
        }
        resp = http.post(
            orders_url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            data=json.dumps(payload)
        )
        data = resp.json()
        order.payment_ref = data.get("id", "")
        order.save(update_fields=["payment_ref"])
        return Response({"id": data.get("id"), "order_id": order.id})

class PaypalCaptureOrder(APIView):
    permission_classes = [IsAuthenticated]

    def _token(self):
        url = "https://api-m.sandbox.paypal.com/v1/oauth2/token" if settings.PAYPAL_ENV == "sandbox" else "https://api-m.paypal.com/v1/oauth2/token"
        r = http.post(url, data={"grant_type": "client_credentials"},
                      auth=(settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET))
        return r.json().get("access_token")

    def post(self, request, order_id):
        token = self._token()
        cap_url = f"https://api-m.sandbox.paypal.com/v2/checkout/orders/{order_id}/capture" if settings.PAYPAL_ENV == "sandbox" else f"https://api-m.paypal.com/v2/checkout/orders/{order_id}/capture"
        resp = http.post(cap_url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        data = resp.json()

        if data.get("status") == "COMPLETED":
            try:
                ord_obj = Order.objects.get(payment_ref=order_id, user=request.user)
                ord_obj.status = "paid"
                ord_obj.save(update_fields=["status"])
                inv = generate_invoice_pdf(ord_obj)
                send_invoice_email(ord_obj)
                # also send order confirmation (thanks + summary)
                fire_and_forget(post_payment_pipeline, ord_obj.id)
                try:
                    locale = getattr(ord_obj.user, "profile", None) and getattr(ord_obj.user.profile, "language", "en")
                    send_order_confirmation(ord_obj, user_locale=locale or "en")
                except Exception:
                    pass
            except Order.DoesNotExist:
                pass

        return Response(data)

# -------------------- Klarna (class-based only) --------------------

VAT_RATE = Decimal("0.19")

class KlarnaCreateSession(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        s = OrderCreateSerializer(data=request.data, context={"request": request})
        s.is_valid(raise_exception=True)
        order = s.save()
        order.payment_method = "klarna"
        order.save(update_fields=["payment_method"])

        items = s.validated_data["items"]
        lines, tax_total = _order_lines(items)
        _, _, total = _compute_from_items(items)

        payload = {
            "purchase_country": settings.KLARNA_COUNTRY,
            "purchase_currency": settings.KLARNA_CURRENCY,
            "locale": settings.KLARNA_LOCALE,
            "order_amount": int(total * 100),
            "order_tax_amount": tax_total,
            "order_lines": lines,
        }
        url = f"{settings.KLARNA_BASE_URL.rstrip('/')}/payments/v1/sessions"
        r = http.post(url, auth=(settings.KLARNA_USERNAME, settings.KLARNA_PASSWORD),
                      headers={"Content-Type": "application/json"},
                      data=json.dumps(payload))
        data = r.json()
        client_token = data.get("client_token")
        return Response({"client_token": client_token, "order_id": order.id})

class KlarnaConfirm(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        order_id = request.data.get("order_id")
        auth_token = request.data.get("authorization_token")
        if not order_id or not auth_token:
            return Response({"detail": "order_id and authorization_token required"}, status=400)

        order = get_object_or_404(Order, pk=order_id, user=request.user)

        items = [{"product_id": oi.product.id, "qty": oi.quantity} for oi in order.items.all()]
        lines, tax_total = _order_lines(items)
        amount = int(order.total_price * 100)

        create_url = f"{settings.KLARNA_BASE_URL.rstrip('/')}/payments/v1/authorizations/{auth_token}/order"
        create_payload = {
            "purchase_country": settings.KLARNA_COUNTRY,
            "purchase_currency": settings.KLARNA_CURRENCY,
            "locale": settings.KLARNA_LOCALE,
            "order_amount": amount,
            "order_tax_amount": tax_total,
            "order_lines": lines,
            "merchant_reference1": order.number,
        }
        cr = http.post(create_url, auth=(settings.KLARNA_USERNAME, settings.KLARNA_PASSWORD),
                       headers={"Content-Type": "application/json"},
                       data=json.dumps(create_payload))
        cdata = cr.json()
        klarna_order_id = cdata.get("order_id")
        if not klarna_order_id:
            return Response({"detail": "Klarna order creation failed", "klarna": cdata}, status=400)

        # (optional capture step skipped or add Order Management capture here)

        order.status = "paid"
        order.payment_ref = klarna_order_id
        order.save(update_fields=["status", "payment_ref"])
        inv = generate_invoice_pdf(order)
        send_invoice_email(order)
        try:
            locale = getattr(order.user, "profile", None) and getattr(order.user.profile, "language", "en")
            send_order_confirmation(order, user_locale=locale or "en")
        except Exception:
            pass
        fire_and_forget(post_payment_pipeline, order.id)
        return Response({"status": "ok", "klarna_order_id": klarna_order_id, "invoice": inv.number})

# -------------------- Bank transfer --------------------

BANK_DETAILS = {
    "iban": "DE35 2415 1116 0000 6827 73",
    "bic": "NOLADE21STK",
    "account_name": "MindMines GbR",
}

class CreateBankTransferOrder(APIView):
   
    permission_classes = [IsAuthenticated]

    def post(self, request):
        s = OrderCreateSerializer(data=request.data, context={"request": request})
        s.is_valid(raise_exception=True)
        order = s.save()
        order.payment_method = "bank"
        order.status = "pending"   # awaiting transfer
        order.save(update_fields=["payment_method", "status"])

        # ✅ email the customer bank instructions + order summary
        try:
            locale = getattr(getattr(order.user, "profile", None), "language", "en") or "en"
            send_bank_transfer_instructions(order, BANK_DETAILS, user_locale=locale)
        except Exception:
            import logging; logging.getLogger(__name__).exception("Bank transfer email failed")

        return Response({
            "order_id": order.id,
            "order_number": order.number,
            "amount": f"{order.total_price:.2f}",
            "iban": BANK_DETAILS["iban"],
            "bic": BANK_DETAILS["bic"],
            "beneficiary": BANK_DETAILS["account_name"],
            "reference": order.number,
            "instructions": "Please transfer the total amount using the reference above within 24 Hours."
        }, status=201)

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
    """Return only within 15 days after delivery.""" 
    if order.status != "delivered": 
        return False, "Returns are only possible after delivery." 
    if not order.delivered_at: 
        return False, "Delivery timestamp missing for this order." 
    
    now = timezone.now() 
    age_days = (now - order.delivered_at).days 
    if age_days > 15: 
        return False, "Return window (15 days) has expired." 
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
            delta = timezone.now() - order.delivered_at
            allowed = (delta.days <= 1)

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
    


    