
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

BERLIN = ZoneInfo("Europe/Berlin")

START_AT = datetime(2026, 11, 13, 0, 0, tzinfo=BERLIN)
END_AT = datetime(2027, 1, 16, 0, 0, tzinfo=BERLIN)

MAX_GIFT_PRICE = Decimal("80.00")
TWO_ITEM_DISCOUNT = Decimal("0.25")


def campaign_active():
    return (
        getattr(settings, "CHRISTMAS_PROMOTIONS_ENABLED", False)
        and START_AT <= timezone.now() < END_AT
    )

def checkout_enabled():
    if not campaign_active() or not getattr(settings, "CHRISTMAS_CHECKOUT_ENABLED", False):
        return False

    applied = MigrationRecorder(connection).applied_migrations()
    return (
        ("server", "0022_christmas_checkout") in applied
        or ("server", "0022_order_promotion_code_order_promotion_discount_and_more") in applied
    )

def vase_product_ids():
    return {
        int(value)
        for value in getattr(
            settings, "CHRISTMAS_BOGO_VASE_IDS", []
        )
    }

def is_christmas_gift_only(product_id):
    return campaign_active() and product_id in (approved_gift_ids() or ())

def approved_gift_ids():
    """
    None means all otherwise-eligible products under
    EUR 80. Configure a list to restrict the range.
    """
    configured = getattr(
        settings, "CHRISTMAS_GIFT_PRODUCT_IDS", None
    )
    if configured is None:
        return None
    return {int(value) for value in configured}
