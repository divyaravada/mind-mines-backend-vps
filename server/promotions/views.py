
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from .config import campaign_active
from .service import eligible_gifts, build_promotion_quote


class QuoteItemSerializer(serializers.Serializer):
    product_id = serializers.IntegerField(min_value=1)
    qty = serializers.IntegerField(
        min_value=1, max_value=99
    )


class QuoteRequestSerializer(serializers.Serializer):
    items = QuoteItemSerializer(many=True)
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

        return Response(quote)
