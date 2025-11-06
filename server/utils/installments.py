# somewhere common, e.g. utils/installments.py
from decimal import Decimal
from django.utils import timezone

def build_installments_schedule(total: Decimal, months: int, first_now: bool = True):
    cents = int(total * 100)
    base = cents // months
    remainder = cents - base * months
    amounts = []
    dates = []
    now = timezone.now()
    for i in range(months):
        amt_cents = base + (remainder if i == months-1 else 0)
        amounts.append(Decimal(amt_cents) / Decimal(100))
        when = now if (first_now and i == 0) else (now + timezone.timedelta(days=30*i))
        dates.append(when)
    return amounts, dates
