from collections import deque
from dataclasses import dataclass
from datetime import date
from math import fsum

from purchase_planning.facts import Segment

MIN_MARKUP_PCT = 25
RECOVERY_MARKUP_PCT = 30


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
) -> tuple[float | None, bool]:
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
    on_review = False
    markup = None
    start = month_index(next_sale.month) if next_sale else last_complete_month
    for month in range(start, last_complete_month + 1):
        while window and month_index(window[0].month) <= month - 12:
            window.popleft()
        while next_sale is not None and month_index(next_sale.month) == month:
            window.append(next_sale)
            next_sale = next(pending, None)
        priced_sales = [(sale.quantity, sale.markup_pct) for sale in window if sale.markup_pct is not None]
        markup = None
        if priced_sales and len(priced_sales) == len(window):
            markup = fsum(quantity * rate for quantity, rate in priced_sales) / fsum(
                quantity for quantity, _ in priced_sales
            )
        if month == last_complete_month and previous_segment is not None:
            on_review = previous_segment == Segment.REVIEW
        if window:
            if markup is None or markup < MIN_MARKUP_PCT:
                on_review = True
            elif markup >= RECOVERY_MARKUP_PCT:
                on_review = False
    return markup, on_review


def classify_sku(
    sales: list[Sale], first_sale: date | None, *, as_of: date, last_complete_month: int,
    had_recent_stock: bool, low_data: bool, previous_segment: Segment | None,
) -> Assortment:
    current_month = month_index(as_of)
    recent_sales = [sale for sale in sales if month_index(sale.month) > current_month - 12]
    sold = fsum(sale.quantity for sale in recent_sales)
    returned = fsum(sale.returns for sale in recent_sales)
    markup, review = _markup_state(sales, last_complete_month, previous_segment)
    flags = []
    missing_cost = any(
        sale.quantity > 0 and sale.markup_pct is None for sale in recent_sales
    )
    if missing_cost or (markup is None and review and sold > 0) or (markup is not None and markup <= 0):
        flags.append("Проверить себестоимость")
    insufficient_markup = sold > 0 and markup is None and not missing_cost and not review
    if insufficient_markup:
        flags.append("Мало данных о наценке")
    if sold > 0 and returned > sold * 0.10:
        flags.append("Возвраты > 10%")

    if sold <= 0 and had_recent_stock:
        segment = Segment.DEAD
    elif sold <= 0 and first_sale is not None:
        segment = Segment.SLEEPING
    elif review or missing_cost:
        segment = Segment.REVIEW
    elif low_data or insufficient_markup or markup == MIN_MARKUP_PCT:
        segment = Segment.CONDITIONAL
    else:
        segment = Segment.PURCHASE
    is_new = first_sale is not None and month_index(first_sale) > current_month - 12
    return Assortment(segment, is_new, markup, sold, tuple(flags))
