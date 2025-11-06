# server/utils/installments.py
from decimal import Decimal
from django.utils import timezone

def build_installments_schedule(total_eur: Decimal, months: int, *, first_now: bool = True):
    """
    Returns (amounts_eur: list[Decimal], dates: list[datetime]).
    Remainder goes to the *last* month.
    """
    total_cents = int(total_eur * 100)
    base = total_cents // months
    rem = total_cents - base * months

    cents_list = [base] * months
    if rem:
        cents_list[-1] += rem

    now = timezone.now()
    dates = []
    for i in range(months):
        # 1st collection "now", then +30d, +60d... (adjust if you prefer month boundaries)
        offset_days = 0 if (i == 0 and first_now) else 30 * (i if first_now else i + 1)
        dates.append(now + timezone.timedelta(days=offset_days))

    amounts_eur = [Decimal(c) / Decimal(100) for c in cents_list]
    return amounts_eur, dates
