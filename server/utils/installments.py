# server/utils/installments.py
from __future__ import annotations
from decimal import Decimal
from typing import List, Tuple
from django.utils import timezone
from dateutil.relativedelta import relativedelta

def split_amount(total: Decimal, months: int) -> List[Decimal]:
    """
    Split total into N monthly parts, last item carries the remainder (exact total preservation).
    Uses cents to avoid floating errors.
    """
    cents = int((total * 100).quantize(Decimal("1")))
    base = cents // months
    rem = cents - base * months
    parts = [Decimal(base) / 100 for _ in range(months)]
    parts[-1] = (Decimal(base + rem) / 100)
    return parts

def monthly_dates(months: int, first_charge_now: bool = True) -> List[timezone.datetime]:
    """
    If first_charge_now=True: today/timestamp for 1st, then +1M, +2M...
    If False: first one is next month (+1M).
    """
    now = timezone.now()
    out = []
    if first_charge_now:
        out.append(now)
        for i in range(1, months):
            out.append(now + relativedelta(months=+i))
    else:
        for i in range(1, months + 1):
            out.append(now + relativedelta(months=+i))
    return out

def build_installments_schedule(total: Decimal, months: int, first_now=True) -> Tuple[List[Decimal], List[timezone.datetime]]:
    return split_amount(total, months), monthly_dates(months, first_charge_now=first_now)
