# server/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.sitemaps.views import sitemap
from django.http import HttpResponse

from .sitemaps import ProductSitemap, CategorySitemap, StaticSitemap
from .feeds import merchant_products_feed

from .views import (
    # Auth / profile
    RegisterView, UserProfileAPIView, AccountAPIView, AccountDeleteRequestView,

    # Addresses
    AddressListCreateAPIView, AddressDetailAPIView, AddressSetDefaultAPIView,

    # Social login
    GoogleLoginAPIView,

    # Password reset & verification
    CustomPasswordResetView, CustomPasswordResetConfirmView,
    verify_email, resend_verification_email,

    # Stones
    NaturalStoneListView, NaturalStoneDetailView, UploadStoneImagesView,
    StoneEnquiryCreateView,

    # Products / Reviews / Search
    ProductListCreate, ProductListView, ProductDetailView, ProductSuggestList,
    ProductReviewListCreate, ReviewListAPIView, ReviewDetailAPIView,

    # Contact / Newsletter
    ContactMessageCreateView, ContactMessageListView,
    NewsletterSubscriberCreateView, NewsletterSubscriberListView,

    # Wishlist
    WishlistView, WishlistDetailView,

    # Orders (user)
    OrderListAPIView, OrderCreateView, OrderDetailAPIView,
    OrderInvoiceAPIView, InvoiceDownloadView,

    # Invoice (public tokenized)
    InvoicePublicDownloadView,

    # Orders (admin helpers)
    OrderAdminUpdateAPIView, SetTrackingAPIView,

    # Coupons / Promo
    CouponPreviewAPIView, ActivePromotionAPIView,

    # Misc
    getRoutes, testEndPoint,

    # PayPal
    PaypalCreateOrder, PaypalCaptureOrder,

    # Stripe
    StripeCreateIntent, StripeCreateCardSetupIntent, StripeCreateSepaSetupIntent,
    StripeCreateSepaIntent, StripeSepaCreateInstallments, stripe_webhook,

    # Bank transfer
    CreateBankTransferOrder,

    # Returns / Cancellations
    OrderRequestReturnAPIView, OrderCancelRequestAPIView,

    # Guest flows
    GuestCheckoutLoginView, GuestOrderLookupView,

    # Stock Alerts
    StockAlertCreateView, StockAlertUnsubscribeView,

    # Admin birthday coupon send
    SendBirthdayCouponAPIView
)

sitemaps = {
    "products": ProductSitemap,
    "categories": CategorySitemap,
    "static": StaticSitemap,
}

def robots_txt(request):
    lines = [
        "User-agent: *",
        "Disallow:",
        "Sitemap: https://mindmines.eu/sitemap.xml",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")

router = DefaultRouter()  # no viewsets registered (ok to keep)

urlpatterns = [
    # -------- Auth / JWT --------
    path("users/register/", RegisterView.as_view(), name="register"),
    path("users/profile/", UserProfileAPIView.as_view(), name="user_profile"),
    path("users/account/", AccountAPIView.as_view(), name="account"),
    path("users/account/delete-request/", AccountDeleteRequestView.as_view(), name="account_delete_request"),

    # -------- Social login --------
    path("auth/google/", GoogleLoginAPIView.as_view(), name="google_login"),

    # -------- Email verify / password reset --------
    path("auth/verify-email/<uidb64>/<token>/", verify_email, name="verify_email"),
    path("auth/resend-verification/", resend_verification_email, name="resend_verification_email"),
    path("auth/password/reset/", CustomPasswordResetView.as_view(), name="password_reset"),
    path("auth/password/reset/confirm/<uidb64>/<token>/", CustomPasswordResetConfirmView.as_view(), name="password_reset_confirm"),

    # -------- Addresses --------
    path("addresses/", AddressListCreateAPIView.as_view(), name="address_list_create"),
    path("addresses/<int:pk>/", AddressDetailAPIView.as_view(), name="address_detail"),
    path("addresses/<int:pk>/set-default/", AddressSetDefaultAPIView.as_view(), name="address_set_default"),

    # -------- Natural stones --------
    path("stones/", NaturalStoneListView.as_view(), name="stone_list"),
    path("stones/<str:type>/", NaturalStoneDetailView.as_view(), name="stone_detail"),
    path("stones/<int:stone_id>/images/", UploadStoneImagesView.as_view(), name="stone_images_upload"),
    path("stones/enquiry/", StoneEnquiryCreateView.as_view(), name="stone_enquiry"),

    # -------- Products --------
    path("products/", ProductListCreate.as_view(), name="product_list_create"),
    path("products/list/", ProductListView.as_view(), name="product_list"),
    path("products/<int:pk>/", ProductDetailView.as_view(), name="product_detail"),
    path("products/suggest/", ProductSuggestList.as_view(), name="product_suggest"),

    # Reviews
    path("products/<int:pk>/reviews/", ProductReviewListCreate.as_view(), name="product_reviews"),
    path("my/reviews/", ReviewListAPIView.as_view(), name="my_reviews"),
    path("reviews/<int:pk>/", ReviewDetailAPIView.as_view(), name="review_detail"),

    # -------- Contact / Newsletter --------
    path("contact/", ContactMessageCreateView.as_view(), name="contact_create"),
    path("contact/admin/", ContactMessageListView.as_view(), name="contact_list_admin"),
    path("newsletter/subscribe/", NewsletterSubscriberCreateView.as_view(), name="newsletter_subscribe"),
    path("newsletter/admin/", NewsletterSubscriberListView.as_view(), name="newsletter_list_admin"),

    # -------- Wishlist --------
    path("wishlist/", WishlistView.as_view(), name="wishlist"),
    path("wishlist/<int:pk>/", WishlistDetailView.as_view(), name="wishlist_item"),

    # -------- Orders (user) --------
    path("orders/", OrderListAPIView.as_view(), name="order_list"),
    path("orders/create/", OrderCreateView.as_view(), name="order_create"),
    path("orders/<int:pk>/", OrderDetailAPIView.as_view(), name="order_detail"),
    path("orders/<int:pk>/invoice/", OrderInvoiceAPIView.as_view(), name="order_invoice"),
    path("orders/<int:pk>/invoice/download/", InvoiceDownloadView.as_view(), name="order_invoice_download"),

    # -------- Invoice (public tokenized) --------
    # FIX: must include pk and name must match reverse() usage in GuestOrderLookupView
    path("orders/<int:pk>/invoice/public/", InvoicePublicDownloadView.as_view(), name="invoice-public-download"),

    # -------- Orders (admin helpers) --------
    path("admin/orders/<int:pk>/update/", OrderAdminUpdateAPIView.as_view(), name="admin_order_update"),
    path("admin/orders/<int:pk>/set-tracking/", SetTrackingAPIView.as_view(), name="admin_set_tracking"),

    # -------- Coupons / Promotion --------
    path("coupons/preview/", CouponPreviewAPIView.as_view(), name="coupon_preview"),
    path("promotions/active/", ActivePromotionAPIView.as_view(), name="active_promotion"),

    # -------- Misc --------
    path("", getRoutes, name="routes"),
    path("test/", testEndPoint, name="test_endpoint"),

    # -------- PayPal --------
    path("paypal/create-order/", PaypalCreateOrder.as_view(), name="paypal_create_order"),
    path("paypal/capture/<str:order_id>/", PaypalCaptureOrder.as_view(), name="paypal_capture_order"),

    # -------- Stripe --------
    path("stripe/create-intent/", StripeCreateIntent.as_view(), name="stripe_create_intent"),
    path("stripe/setup-intent/card/", StripeCreateCardSetupIntent.as_view(), name="stripe_setup_card"),
    path("stripe/setup-intent/sepa/", StripeCreateSepaSetupIntent.as_view(), name="stripe_setup_sepa"),
    path("stripe/create-intent/sepa/", StripeCreateSepaIntent.as_view(), name="stripe_create_sepa_intent"),
    path("stripe/installments/sepa/", StripeSepaCreateInstallments.as_view(), name="stripe_sepa_installments"),
    path("stripe/webhook/", stripe_webhook, name="stripe_webhook"),

    # -------- Bank transfer --------
    path("bank-transfer/create-order/", CreateBankTransferOrder.as_view(), name="bank_transfer_create_order"),

    # -------- Returns / Cancellations --------
    path("orders/<int:pk>/request-return/", OrderRequestReturnAPIView.as_view(), name="order_request_return"),
    path("orders/<int:pk>/cancel-request/", OrderCancelRequestAPIView.as_view(), name="order_cancel_request"),

    # -------- Guest flows --------
    path("guest/login/", GuestCheckoutLoginView.as_view(), name="guest_login"),
    path("guest/order-lookup/", GuestOrderLookupView.as_view(), name="guest_order_lookup"),

    # -------- Allauth (optional) --------
    path("accounts/", include("allauth.urls")),

    # -------- Stock alerts --------
    path("stock-alerts/", StockAlertCreateView.as_view(), name="stock-alerts-create"),
    path("stock-alerts/unsubscribe/<str:unsubscribe_token>/", StockAlertUnsubscribeView.as_view(), name="stock-alerts-unsubscribe"),

    # -------- Admin birthday coupon send --------
    path("admin/send-birthday-coupon/", SendBirthdayCouponAPIView.as_view(), name="admin_send_birthday_coupon"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

urlpatterns += [
    path("sitemap.xml", sitemap, {"sitemaps": sitemaps}, name="django.contrib.sitemaps.views.sitemap"),
    path("robots.txt", robots_txt),
    path("feeds/google-merchant.json", merchant_products_feed),
]

urlpatterns += router.urls
