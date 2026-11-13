from decimal import Decimal
from shlex import quote
from django.db.migrations import serializer
from django.db.migrations import serializer
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from server.serializers import OrderCreateSerializer
from .config import campaign_active, checkout_enabled
from .service import eligible_gifts, build_promotion_quote


class QuoteItemSerializer(serializers.Serializer):
    product_id = serializers.IntegerField(min_value=1)
    qty = serializers.IntegerField(
        min_value=1, max_value=99
    )


class QuoteRequestSerializer(serializers.Serializer):
    items = QuoteItemSerializer(many=True)
    coupon_code = serializers.CharField(max_length=64, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    gift_product_id = serializers.IntegerField(
        min_value=1,
        required=False,
        allow_null=True,
    )


class ChristmasGiftAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        if not campaign_active():
            return Response({
                "active": False,
                "products": [],
            })

        products = []

        for product in eligible_gifts():
            image = ""
            if product.image:
                image = request.build_absolute_uri(
                    product.image.url
                )

            products.append({
                "id": product.pk,
                "title_en": product.title_en,
                "title_de": product.title_de,
                "image": image,
                "price_gross": str(product.price_gross),
                "final_price_gross": str(
                    product.final_price_gross
                ),
                "available": True,
            })

        return Response({
            "active": True,
            "checkout_enabled": checkout_enabled(),
            "currency": "EUR",
            "max_gift_price_exclusive": "80.00",
            "products": products,
        })


class ChristmasQuoteAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        if not campaign_active():
            return Response(
                {"detail": "Promotion is not active."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = QuoteRequestSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        quote = build_promotion_quote(
            items=serializer.validated_data["items"],
            gift_product_id=serializer.validated_data.get(
                "gift_product_id"
            ),
        )
        coupon_code = serializer.validated_data.get("coupon_code", "")
        if coupon_code:
            quote_serializer = OrderCreateSerializer()
            subtotal = Decimal(quote["subtotal"])
            coupon_total, coupon_discount, coupon = quote_serializer._apply_coupon(
                subtotal, coupon_code, request.user, serializer.validated_data["items"],
                serializer.validated_data.get("email", ""),
            )
            if coupon and (
                not quote["offer_code"] or coupon_discount > Decimal(quote["customer_benefit"])
            ):
                quote.update({
                    "offer_code": None,
                    "gift_product_id": None,
                    "gift_value": "0.00",
                    "customer_benefit": str(coupon_discount),
                    "promotion_discount": "0.00",
                    "total_before_shipping": str(coupon_total),
                   "coupon_discount": str(coupon_discount),
                    "coupon_code": coupon.code,
                })
        return Response(quote)
