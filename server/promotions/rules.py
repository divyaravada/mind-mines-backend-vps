
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from .config import TWO_ITEM_DISCOUNT

CENT = Decimal("0.01")

def money(value):
    return Decimal(str(value)).quantize(
        CENT, rounding=ROUND_HALF_UP
    )


@dataclass(frozen=True)
class CartLine:
    product_id: int
    quantity: int
    unit_price: Decimal
    stock: int


@dataclass(frozen=True)
class Offer:
    code: str
    discount: Decimal
    gift_product_id: int
    gift_value: Decimal
    benefit: Decimal


PRIORITY = {
    "VASE_BOGO": 3,
    "TWO_ITEM_GIFT": 2,
    "SINGLE_ITEM_GIFT": 1,
}


def calculate_best_offer(
    lines,
    vase_ids,
    gift=None,
):
    """
    Calculate the best eligible offer.

    lines: list[CartLine]
    vase_ids: set[int]
    gift: dict with id, price, stock or None

    No database writes.
    No payment operations.
    """
    if not lines:
        return None

    for line in lines:
        if line.quantity < 1:
            raise ValueError("Invalid item quantity.")

        if line.unit_price <= 0:
            raise ValueError("Invalid product price.")

        if line.stock < line.quantity:
            raise ValueError(
                f"Insufficient stock for {line.product_id}"
            )

    candidates = []

    # 1. Identical vase BOGO — one extra unit/order
    for line in lines:
        if (
            line.product_id in vase_ids
            and line.stock >= line.quantity + 1
        ):
            value = money(line.unit_price)

            candidates.append(
                Offer(
                    code="VASE_BOGO",
                    discount=Decimal("0.00"),
                    gift_product_id=line.product_id,
                    gift_value=value,
                    benefit=value,
                )
            )

    # Gift-based promotions require selected gift.
    if gift is not None:
        gift_id = int(gift["id"])
        gift_price = money(gift["price"])
        gift_stock = int(gift["stock"])

        if not (
            Decimal("0.00")
            < gift_price
            < Decimal("80.00")
        ):
            raise ValueError("Gift is not eligible.")

        paid_qty_of_gift = sum(
            line.quantity
            for line in lines
            if line.product_id == gift_id
        )

        if gift_stock < paid_qty_of_gift + 1:
            raise ValueError("Gift is out of stock.")

        # 2. One-item offer
        candidates.append(
            Offer(
                code="SINGLE_ITEM_GIFT",
                discount=Decimal("0.00"),
                gift_product_id=gift_id,
                gift_value=gift_price,
                benefit=gift_price,
            )
        )

        # 3. Two-item promotion
        # Expand paid units, not just unique SKUs.
        unit_prices = sorted(
            (
                money(line.unit_price)
                for line in lines
                for _ in range(line.quantity)
            ),
            reverse=True,
        )

        if len(unit_prices) >= 2:
            # Apply 25% to the two highest-priced
            # qualifying paid units only.
            discount = sum(
                (
                    money(price * TWO_ITEM_DISCOUNT)
                    for price in unit_prices[:2]
                ),
                Decimal("0.00"),
            )
            discount = money(discount)

            candidates.append(
                Offer(
                    code="TWO_ITEM_GIFT",
                    discount=discount,
                    gift_product_id=gift_id,
                    gift_value=gift_price,
                    benefit=money(
                        discount + gift_price
                    ),
                )
            )

    if not candidates:
        return None

    return max(
        candidates,
        key=lambda offer: (
            offer.benefit,
            PRIORITY[offer.code],
        ),
    )


def quote_totals(lines, offer):
    subtotal = money(sum(
        (
            money(line.unit_price) * line.quantity
            for line in lines
        ),
        Decimal("0.00"),
    ))

    discount = (
        money(offer.discount)
        if offer else Decimal("0.00")
    )

    return {
        "subtotal": str(subtotal),
        "promotion_discount": str(discount),
        "total_before_shipping": str(
            money(max(Decimal("0.00"), subtotal - discount))
        ),
    }
