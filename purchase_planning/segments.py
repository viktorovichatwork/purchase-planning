from collections import deque
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from math import fsum

from purchase_planning.facts import Segment

REVIEW_MARKUP_PCT = 20
PURCHASE_MARKUP_PCT = 25
RECOVERY_MARKUP_PCT = 30
ASSORTMENT_MONTHS = 12
RETURN_FLAG_SHARE = 0.10
MISSING_COST_SHARE = 0.50


class ReviewReason(StrEnum):
    MISSING_COST = f"Себестоимость неизвестна более чем для {MISSING_COST_SHARE:.0%} проданных штук"
    LOW_MARKUP = "Наценка ниже 20%"
    HYSTERESIS = "Гистерезис: наценка ниже 30%"


def month_index(day: date) -> int:
    return day.year * 12 + day.month


@dataclass(frozen=True)
class Sale:
    month: date
    quantity: float
    returns: float
    markup_pct: float | None
    partial: bool


@dataclass(frozen=True)
class Assortment:
    segment: Segment
    is_new: bool
    markup_pct_12m: float | None
    sales_qty_12m: float
    flags: tuple[str, ...]


def _markup_state(
    sales: list[Sale], last_complete_month: int, previous_segment: Segment | None,
) -> tuple[float | None, float, ReviewReason | None]:
    window: deque[Sale] = deque()
    complete_sales = sorted(
        (
            sale for sale in sales
            if sale.quantity > 0 and not sale.partial and month_index(sale.month) <= last_complete_month
        ),
        key=lambda sale: sale.month,
    )
    pending = iter(complete_sales)
    next_sale = next(pending, None)
    reason = None
    markup = None
    start = month_index(next_sale.month) if next_sale else last_complete_month
    for month in range(start, last_complete_month + 1):
        while window and month_index(window[0].month) <= month - ASSORTMENT_MONTHS:
            window.popleft()
        while next_sale is not None and month_index(next_sale.month) == month:
            window.append(next_sale)
            next_sale = next(pending, None)
        priced_sales = [(sale.quantity, sale.markup_pct) for sale in window if sale.markup_pct is not None]
        priced_units = fsum(quantity for quantity, _ in priced_sales)
        missing_units = fsum(sale.quantity for sale in window if sale.markup_pct is None)
        markup = None
        if priced_units > 0:
            markup = fsum(quantity * rate for quantity, rate in priced_sales) / priced_units
        was_on_review = reason is not None
        if month == last_complete_month and previous_segment is not None:
            was_on_review = previous_segment == Segment.REVIEW
        if missing_units * (1 - MISSING_COST_SHARE) > priced_units * MISSING_COST_SHARE:
            reason = ReviewReason.MISSING_COST
        elif markup is not None and markup < REVIEW_MARKUP_PCT:
            reason = ReviewReason.LOW_MARKUP
        elif was_on_review and (markup is None or markup < RECOVERY_MARKUP_PCT):
            reason = ReviewReason.HYSTERESIS
        else:
            reason = None
    return markup, missing_units, reason


def classify_sku(
    sales: list[Sale], first_sale: date | None, *, as_of: date, last_complete_month: int,
    had_recent_stock: bool, low_data: bool, previous_segment: Segment | None,
) -> Assortment:
    current_month = month_index(as_of)
    recent_sales = [sale for sale in sales if month_index(sale.month) > current_month - ASSORTMENT_MONTHS]
    sold = fsum(sale.quantity for sale in recent_sales)
    returned = fsum(sale.returns for sale in recent_sales)
    markup, missing_units, review_reason = _markup_state(sales, last_complete_month, previous_segment)
    flags = []
    if missing_units > 0 or (markup is not None and markup <= 0):
        flags.append("Проверить себестоимость")
    insufficient_markup = sold > 0 and markup is None and review_reason is None
    if insufficient_markup:
        flags.append("Мало данных о наценке")
    if sold > 0 and returned > sold * RETURN_FLAG_SHARE:
        flags.append(f"Возвраты > {RETURN_FLAG_SHARE:.0%}")

    if sold <= 0 and had_recent_stock:
        segment = Segment.DEAD
    elif sold <= 0 and first_sale is not None:
        segment = Segment.SLEEPING
    elif review_reason is not None:
        segment = Segment.REVIEW
        flags.append(review_reason)
    elif low_data or insufficient_markup or (markup is not None and markup < PURCHASE_MARKUP_PCT):
        segment = Segment.CONDITIONAL
    else:
        segment = Segment.PURCHASE
    is_new = first_sale is not None and month_index(first_sale) > current_month - ASSORTMENT_MONTHS
    return Assortment(segment, is_new, markup, sold, tuple(flags))
