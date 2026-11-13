
from collections import Counter
from decimal import Decimal

from rest_framework.exceptions import ValidationError

from server.models import Product

from .config import (
    MAX_GIFT_PRICE,
    vase_product_ids,
    approved_gift_ids,
)
from .rules import (
    CartLine,
    calculate_best_offer,
    quote_totals,
)


def eligible_gifts():
    """
    Return in-stock products with BOTH normal and
    current selling prices below EUR 80.
    """
    queryset = Product.objects.filter(
        price_gross__gt=0,
        price_gross__lt=MAX_GIFT_PRICE,
        quantity__gt=0,
    ).order_by("price_gross", "id")

    approved = approved_gift_ids()
    if approved is not None:
        queryset = queryset.filter(pk__in=approved)

    return [
        product for product in queryset
        if Decimal(product.final_price_gross) > 0
        and Decimal(product.final_price_gross)
            < MAX_GIFT_PRICE
    ]


def build_promotion_quote(items, gift_product_id=None, *, locked_products=None):
    """
    items = [
        {"product_id": 12, "qty": 1},
        {"product_id": 18, "qty": 1}
    ]
    """
    if not items or len(items) > 50:
        raise ValidationError(
            {"items": "Provide 1 to 50 cart lines."}
        )

    quantities = Counter()

    for item in items:
        if not isinstance(item, dict):
            raise ValidationError("Invalid cart item.")

        try:
            product_id = int(item["product_id"])
            qty = int(item["qty"])
        except (KeyError, TypeError, ValueError):
            raise ValidationError("Invalid cart item.")

        if product_id <= 0 or qty < 1 or qty > 99:
            raise ValidationError("Invalid quantity or product.")

        quantities[product_id] += qty

        if quantities[product_id] > 99:
            raise ValidationError(
                "Maximum quantity exceeded."
            )

    if locked_products is None:
        product_map = Product.objects.in_bulk(quantities)
    else:
        product_map = {
            pk: locked_products[pk] for pk in quantities if pk in locked_products
        }

    if len(product_map) != len(quantities):
        raise ValidationError(
            "One or more products are unavailable."
        )

    lines = []

    for product_id, qty in quantities.items():
        product = product_map[product_id]

        if product.quantity < qty:
            raise ValidationError(
                f"Insufficient stock for {product_id}."
            )

        lines.append(
            CartLine(
                product_id=product_id,
                quantity=qty,
                unit_price=Decimal(
                    product.final_price_gross
                ),
                stock=product.quantity,
            )
        )

    selected_gift = None

    if gift_product_id is not None:
        try:
            gift_id = int(gift_product_id)
        except (TypeError, ValueError):
            raise ValidationError(
                "Invalid gift product ID."
            )

        product = (
            Product.objects.filter(pk=gift_id).first()
            if locked_products is None else locked_products.get(gift_id)
        )
        approved = approved_gift_ids()
        if (
            product is None
            or (approved is not None and gift_id not in approved)
            or not 0 < product.price_gross < MAX_GIFT_PRICE
            or not 0 < product.final_price_gross < MAX_GIFT_PRICE
            or product.quantity < 1
        ):
            raise ValidationError(
                "Selected gift is not eligible."
            )

        selected_gift = {
            "id": product.pk,
            "price": Decimal(
                product.final_price_gross
            ),
            "stock": product.quantity,
        }

    try:
        offer = calculate_best_offer(
            lines,
            vase_product_ids(),
            gift=selected_gift,
        )
    except ValueError as exc:
        raise ValidationError(str(exc))

    totals = quote_totals(lines, offer)

    return {
        "currency": "EUR",
        "offer_code": offer.code if offer else None,
        "gift_product_id": (
            offer.gift_product_id if offer else None
        ),
        "gift_value": (
            str(offer.gift_value) if offer else "0.00"
        ),
        "customer_benefit": (
            str(offer.benefit) if offer else "0.00"
        ),
        **totals,
        "is_provisional": True,
    }
