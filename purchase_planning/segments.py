from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from math import fsum

from purchase_planning.facts import Segment

PURCHASE_MARKUP_PCT = 25
ASSORTMENT_MONTHS = 12
RETURN_FLAG_SHARE = 0.10
MISSING_COST_SHARE = 0.50


class ReviewReason(StrEnum):
    MISSING_COST = f"Себестоимость неизвестна более чем для {MISSING_COST_SHARE:.0%} проданных штук"
    LOW_MARKUP = "Наценка ниже порога"


def month_index(day: date) -> int:
    return day.year * 12 + day.month


@dataclass(frozen=True)
class Sale:
    month: date
    quantity: float
    returns: float
    markup_pct: float | None
    partial: bool

    @property
    def net_quantity(self) -> float:
        return max(0.0, self.quantity - self.returns)


@dataclass(frozen=True)
class Assortment:
    segment: Segment
    is_new: bool
    markup_pct_12m: float | None
    sales_qty_12m: float
    flags: tuple[str, ...]


def in_assortment_window(month: date, *, as_of: date, last_complete_month: int) -> bool:
    """The last 12 complete months plus the current incomplete one.

    The ERP counts sku_dim.avg_markup_pct_12m over the same months. A sale in the incomplete month is a real sale,
    and a markup percentage does not depend on how many days of the month are in the export.
    """
    return last_complete_month - ASSORTMENT_MONTHS < month_index(month) <= month_index(as_of)


def _markup_state(
    sales: list[Sale], *, as_of: date, last_complete_month: int,
) -> tuple[float | None, float, ReviewReason | None]:
    """Markup over the assortment window, weighted by units, and why the SKU is not ordered if so."""
    window = [
        sale for sale in sales
        if sale.quantity > 0 and in_assortment_window(sale.month, as_of=as_of, last_complete_month=last_complete_month)
    ]
    priced_sales = [(sale.quantity, sale.markup_pct) for sale in window if sale.markup_pct is not None]
    priced_units = fsum(quantity for quantity, _ in priced_sales)
    missing_units = fsum(sale.quantity for sale in window if sale.markup_pct is None)
    markup = fsum(quantity * rate for quantity, rate in priced_sales) / priced_units if priced_units > 0 else None
    if missing_units * (1 - MISSING_COST_SHARE) > priced_units * MISSING_COST_SHARE:
        return markup, missing_units, ReviewReason.MISSING_COST
    if markup is not None and markup < PURCHASE_MARKUP_PCT:
        return markup, missing_units, ReviewReason.LOW_MARKUP
    return markup, missing_units, None


def classify_sku(
    sales: list[Sale], first_sale: date | None, *, as_of: date, last_complete_month: int,
    had_recent_stock: bool, low_data: bool,
) -> Assortment:
    window = {"as_of": as_of, "last_complete_month": last_complete_month}
    recent_sales = [sale for sale in sales if in_assortment_window(sale.month, **window)]
    sold = fsum(sale.quantity for sale in recent_sales)
    returned = fsum(sale.returns for sale in recent_sales)
    markup, missing_units, review_reason = _markup_state(sales, **window)
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
    elif low_data or insufficient_markup:
        segment = Segment.CONDITIONAL
    else:
        segment = Segment.PURCHASE
    is_new = first_sale is not None and in_assortment_window(first_sale, **window)
    return Assortment(segment, is_new, markup, sold, tuple(flags))
