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
    LoginStartAPIView, LoginVerifyAPIView, ProductDemandSummaryAPIView, RegisterView, UserProfileAPIView, AccountAPIView, AccountDeleteRequestView,

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

    # Cart
    CartSyncView, AdminCartListView,

    # Contact / Newsletter
    ContactMessageCreateView, ContactMessageListView,
    NewsletterSubscriberCreateView, NewsletterSubscriberListView,

    # Wishlist
    WishlistView, WishlistDetailView,

    # Orders (user)
    OrderListAPIView, OrderCreateView, OrderDetailAPIView,OrderDetailPublicAPIView,
    OrderInvoiceAPIView, InvoiceDownloadView,

    # Orders (admin helpers)
    OrderAdminUpdateAPIView, SetTrackingAPIView,

    # Coupons / Promo
    CouponPreviewAPIView, ActivePromotionAPIView,

    # Misc
    getRoutes, testEndPoint,

    # Digital marketing
    MarketingEventTrackView, 
    

    # PayPal
    PaypalCreateOrder, PaypalCaptureOrder,

    # Stripe
    StripeCreateIntent, StripeCardInstallmentsView,StripeCreateRedirectIntent,StripeCreateSetupIntent,
    StripeCreateSepaIntent, StripeSepaInstallmentsView,StripeCreateSepaSetupIntent,
    SepaPostConfirmView, stripe_webhook, OrderPaymentStatusUpdateAPIView,

    # Returns / Cancellations
    OrderRequestReturnAPIView, OrderCancelRequestAPIView,

    # Stock Alerts / On-demand
    StockAlertCreateView, StockAlertUnsubscribeView, OnDemandOrderCreateView,

    # Admin birthday coupon send
    SendBirthdayCouponAPIView,

    # Guest lookup / invoices
    GuestOrderLookupAPIView, GuestInvoiceDownloadView,
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

router = DefaultRouter()  # (no viewsets yet)

urlpatterns = [
    # -------- Auth / Account --------
    path("users/register/", RegisterView.as_view(), name="register"),
    path("users/profile/", UserProfileAPIView.as_view(), name="user_profile"),
    path("users/account/", AccountAPIView.as_view(), name="account"),
    path("users/account/delete-request/", AccountDeleteRequestView.as_view(), name="account_delete_request"),
    path("users/login/start/", LoginStartAPIView.as_view(), name="login_start"),
    path("users/login/verify/", LoginVerifyAPIView.as_view(), name="login_verify"),
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
    path("admin/analytics/product-demand/", ProductDemandSummaryAPIView.as_view(), name="admin-product-demand"),

    #cart
    path("cart/", CartSyncView.as_view(), name="cart-sync"),
    path("admin/carts/", AdminCartListView.as_view(), name="admin-cart-list"),

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
    path("orders/public/<int:pk>/", OrderDetailPublicAPIView.as_view(), name="order_detail_public"),
    path("orders/<int:pk>/invoice/", OrderInvoiceAPIView.as_view(), name="order_invoice"),
    path("orders/<int:pk>/invoice/download/", InvoiceDownloadView.as_view(), name="order_invoice_download"),

    # -------- Orders (admin helpers) --------
    path("admin/orders/<int:pk>/update/", OrderAdminUpdateAPIView.as_view(), name="admin_order_update"),
    path("admin/orders/<int:pk>/set-tracking/", SetTrackingAPIView.as_view(), name="admin_set_tracking"),

    # -------- Coupons / PromotionBanner --------
    path("coupons/preview/", CouponPreviewAPIView.as_view(), name="coupon_preview"),
    path("promotions/active/", ActivePromotionAPIView.as_view(), name="active_promotion"),

    # -------- Misc --------
    path("", getRoutes, name="routes"),
    path("test/", testEndPoint, name="test_endpoint"),

    # -------- Digital marketing / tracking --------
    path("marketing/track/", MarketingEventTrackView.as_view(), name="marketing_track"),

    # -------- PayPal --------
    path("paypal/create-order/", PaypalCreateOrder.as_view(), name="paypal_create_order"),
    path("paypal/capture/<str:order_id>/", PaypalCaptureOrder.as_view(), name="paypal_capture_order"),

    # -------- Stripe --------
    
    path("stripe/create-intent/", StripeCreateIntent.as_view(), name="stripe_create_intent_card_compat"),  # back-compat
    path("stripe/create-intent/redirect/", StripeCreateRedirectIntent.as_view(), name="stripe_create_redirect_intent"),
    path("stripe/create-intent/sepa/", StripeCreateSepaIntent.as_view(), name="stripe_create_sepa_intent"),  # optional no-slash
    path("stripe/setup-intent/", StripeCreateSetupIntent.as_view(), name="stripe_setup_intent"),
    path("stripe/installments/card/", StripeCardInstallmentsView.as_view(), name="stripe_installments_card"),
    path("stripe/setup-intent/sepa/", StripeCreateSepaSetupIntent.as_view(), name="stripe_setup_intent_sepa"),
    path("stripe/installments/sepa/", StripeSepaInstallmentsView.as_view(), name="stripe_installments_sepa"),
    path("payments/sepa-post-confirm/", SepaPostConfirmView.as_view(), name="sepa-post-confirm"),
    path("stripe/webhook/", stripe_webhook, name="stripe_webhook"),
    path("stripe/webhook", stripe_webhook, name="stripe_webhook_no_slash"),
    
    path("orders/<int:pk>/payment-status/", OrderPaymentStatusUpdateAPIView.as_view(), name="order_payment_status"),

    # -------- Returns / Cancellations --------
    path("orders/<int:pk>/request-return/", OrderRequestReturnAPIView.as_view(), name="order_request_return"),
    path("orders/<int:pk>/cancel-request/", OrderCancelRequestAPIView.as_view(), name="order_cancel_request"),

    # -------- Allauth (optional) --------
    path("accounts/", include("allauth.urls")),

    # -------- Stock alerts / On-demand --------
    path("stock-alerts/", StockAlertCreateView.as_view(), name="stock-alerts-create"),
    path("stock-alerts/unsubscribe/<str:unsubscribe_token>/", StockAlertUnsubscribeView.as_view(), name="stock-alerts-unsubscribe"),
    path("on-demand/", OnDemandOrderCreateView.as_view(), name="on-demand-create"),

    # -------- Admin birthday coupon send --------
    path("admin/send-birthday-coupon/", SendBirthdayCouponAPIView.as_view(), name="admin_send_birthday_coupon"),

    # -------- Guest order lookup / invoice download --------
    path("orders/guest-lookup/", GuestOrderLookupAPIView.as_view(), name="guest_order_lookup"),
    # aligned with build_guest_invoice_download_url()
    path("orders/guest-invoice/", GuestInvoiceDownloadView.as_view(), name="guest_invoice_download"),
]

# Static/media only during development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

urlpatterns += [
    path("sitemap.xml", sitemap, {"sitemaps": sitemaps}, name="django.contrib.sitemaps.views.sitemap"),
    path("robots.txt", robots_txt),
    path("feeds/google-merchant.json", merchant_products_feed),
]

urlpatterns += router.urls
