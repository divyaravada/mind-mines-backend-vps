
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from django.conf import settings
from django.conf.urls.static import static
from .views import (
    WishlistView, WishlistDetailView, TestPostView, UploadStoneImagesView,
    NaturalStoneDetailView, NaturalStoneListView, StoneEnquiryCreateView,
    FacebookLoginAPIView, ReviewListAPIView, OrderListAPIView, UserProfileAPIView,
    AddressListCreateAPIView, RegisterView, GoogleLoginAPIView, verify_email,
    resend_verification_email, testEndPoint, getRoutes, CustomPasswordResetView,
    CustomPasswordResetConfirmView, ProductListCreate, 
    ProductReviewListCreate, ProductDetailView, ContactMessageCreateView,
    NewsletterSubscriberCreateView, ContactMessageListView,ReviewDetailAPIView,
    NewsletterSubscriberListView,AccountAPIView, AddressDetailAPIView,AddressSetDefaultAPIView,
     PaypalCreateOrder, PaypalCaptureOrder,OrderCreateView,
     InvoiceDownloadView, OrderAdminUpdateAPIView,AccountDeleteRequestView,
     CreateBankTransferOrder, StoneEnquiryCreateView,OrderDetailAPIView,SetTrackingAPIView,OrderInvoiceAPIView,
   CouponPreviewAPIView,ActivePromotionAPIView,StripeCreateSepaIntent,StripeCreateSepaSetupIntent,StripeSepaCreateInstallments,
    stripe_webhook,StripeCreateIntent,StripeCreateCardSetupIntent,
     OrderCancelRequestAPIView,OrderRequestReturnAPIView,ProductSuggestList
)
from django.contrib.sitemaps.views import sitemap
from .sitemaps import ProductSitemap, CategorySitemap, StaticSitemap
from django.http import HttpResponse
from .feeds import merchant_products_feed

sitemaps = {
    "products": ProductSitemap,
    "categories": CategorySitemap,
    "static": StaticSitemap,
}

def robots_txt(request):
    lines = [
        "User-agent: *",
        "Disallow:",
        "Sitemap: https://YOURDOMAIN.com/sitemap.xml",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")

router = DefaultRouter()

urlpatterns = [
    path("users/register/", RegisterView.as_view(), name="name_register"),
    path("accounts/", include("allauth.urls")),
    path("users/auth/google/", GoogleLoginAPIView.as_view(), name="google_login"),
    path("users/login/facebook/", FacebookLoginAPIView.as_view(), name="facebook-login"),
    path("users/verify-email/<uidb64>/<token>/", verify_email, name="verify_email"),
    path("users/resend-verification/", resend_verification_email),
    path("users/test/", testEndPoint, name="test"),
    path("", getRoutes),
    path("users/reset-password/", CustomPasswordResetView.as_view(), name="password-reset"),
    path("users/reset-password/<uidb64>/<token>/", CustomPasswordResetConfirmView.as_view(), name="password-reset-confirm"),

    path("natural-stone/enquiry/", StoneEnquiryCreateView.as_view(), name="stone-enquiry"),
    path("natural-stone/test-post/", TestPostView.as_view(), name="test-post"),
    path("natural-stone/", NaturalStoneListView.as_view()),
    path("natural-stone/<str:type>/", NaturalStoneDetailView.as_view()),
    path("natural-stone/<int:stone_id>/upload-images/", UploadStoneImagesView.as_view()),

    path("decorative-products/", ProductListCreate.as_view()),
    path("decorative-products/<int:pk>/", ProductDetailView.as_view(), name="product-detail"),
    path("decorative-products/<int:pk>/reviews/", ProductReviewListCreate.as_view(), name="product-reviews"),
    path("reviews/<int:pk>/", ReviewDetailAPIView.as_view()),
    path("decorative-products/suggest/", ProductSuggestList.as_view()),

    path("contact/", ContactMessageCreateView.as_view(), name="contact"),
    path("contact-list/", ContactMessageListView.as_view(), name="contact-list"),
    path("newsletter/", NewsletterSubscriberCreateView.as_view(), name="newsletter"),
    path("newsletter-list/", NewsletterSubscriberListView.as_view(), name="newsletter-list"),

    path("dashboard/profile/", UserProfileAPIView.as_view()),
    path("dashboard/wishlist/", WishlistView.as_view(), name="wishlist"),
    path("dashboard/wishlist/<int:pk>/", WishlistDetailView.as_view(), name="wishlist-detail"),
    path("dashboard/orders/", OrderListAPIView.as_view()),
    path("dashboard/orders/<int:pk>/", OrderDetailAPIView.as_view(), name="order-detail"),
    path("dashboard/orders/<int:pk>/invoice/", OrderInvoiceAPIView.as_view(), name="order-invoice"),
    path("dashboard/reviews/", ReviewListAPIView.as_view()),
    path("dashboard/account/", AccountAPIView.as_view()),
    path("dashboard/account/delete-request/", AccountDeleteRequestView.as_view()),
    path("dashboard/addresses/", AddressListCreateAPIView.as_view()),
    path("dashboard/addresses/<int:pk>/", AddressDetailAPIView.as_view()),
    path("dashboard/addresses/<int:pk>/set-default/", AddressSetDefaultAPIView.as_view()),

     # Orders
    path("orders/create/", OrderCreateView.as_view()),
    path("orders/<int:pk>/set-tracking/", SetTrackingAPIView.as_view()),
    path("admin/orders/<int:pk>/", OrderAdminUpdateAPIView.as_view()),
    path("orders/<int:pk>/request-cancel/", OrderCancelRequestAPIView.as_view()),
    path("orders/<int:pk>/request-return/", OrderRequestReturnAPIView.as_view()),



     # PayPal
    path("payments/paypal/create-order/", PaypalCreateOrder.as_view()),
    path("payments/paypal/capture/<str:order_id>/", PaypalCaptureOrder.as_view()),

    #Stripe
    path("payments/stripe/create-intent/", StripeCreateIntent.as_view()),
    path("payments/stripe/sepa/create-setup-intent/", StripeCreateSepaSetupIntent.as_view()),
    path("payments/stripe/sepa/create-intent/", StripeCreateSepaIntent.as_view()),
    path("payments/stripe/create-installments/", StripeSepaCreateInstallments.as_view()),
    path("payments/stripe/webhook/", stripe_webhook),
    path("payments/stripe/card/create-setup-intent/", StripeCreateCardSetupIntent.as_view()),



    # Bank Transfer
    path("payments/bank/create/", CreateBankTransferOrder.as_view()),

    # Invoice
    path("orders/<int:pk>/invoice/", InvoiceDownloadView.as_view()),

    #Coupon
    path("coupons/apply/", CouponPreviewAPIView.as_view()),

    #Promotion
    path("promos/active/", ActivePromotionAPIView.as_view()),
]
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

urlpatterns += [
    path("sitemap.xml", sitemap, {"sitemaps": sitemaps}, name="django.contrib.sitemaps.views.sitemap"),
    path("robots.txt", robots_txt),
    path("feeds/google-merchant.json", merchant_products_feed)
]

urlpatterns += router.urls
