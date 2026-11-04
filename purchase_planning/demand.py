from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from math import isclose
from statistics import mean, median, pvariance

from purchase_planning.facts import CategoryProfile, DemandType

MONTHS_PER_YEAR = 12
PROFILE_START_YEAR = 2020
PROFILE_END_YEAR = 2025
ROUNDING_TOLERANCE = 1e-15
SEASON_PEAK_MONTHS = 3
SEASON_PEAK_SHARE = 0.5
SEASON_WINDOW_SHARE = 0.8
MIN_OWN_DEMAND_MONTHS = 3
SBC_ADI_THRESHOLD = 1.32
SBC_CV2_THRESHOLD = 0.49


@dataclass(frozen=True)
class SkuDemand:
    annual_level: float | None
    excluded_stockout_months: int
    low_data: bool
    eligible_months: int
    demand_type: DemandType


def _demand_type(quantities: list[float]) -> DemandType:
    positive = [quantity for quantity in quantities if quantity > 0]
    if len(positive) < 2:
        return DemandType.INTERMITTENT
    adi = len(quantities) / len(positive)
    cv2 = pvariance(positive) / mean(positive) ** 2
    if adi < SBC_ADI_THRESHOLD and cv2 < SBC_CV2_THRESHOLD:
        return DemandType.REGULAR
    return DemandType.INTERMITTENT


def _season_window(monthly_totals: list[float], annual_total: float) -> tuple[int, ...]:
    # Doubling the calendar lets consecutive windows cross December.
    calendar = monthly_totals * 2
    starts = range(MONTHS_PER_YEAR)
    peak = max(sum(calendar[start:start + SEASON_PEAK_MONTHS]) for start in starts)
    half = annual_total * SEASON_PEAK_SHARE
    if peak < half and not isclose(peak, half, rel_tol=ROUNDING_TOLERANCE):
        return ()

    threshold = annual_total * SEASON_WINDOW_SHARE
    for length in range(1, MONTHS_PER_YEAR + 1):
        window_totals = [sum(calendar[start:start + length]) for start in starts]
        highest_total = max(window_totals)
        if highest_total >= threshold or isclose(highest_total, threshold, rel_tol=ROUNDING_TOLERANCE):
            start = next(
                month for month, total in enumerate(window_totals)
                if isclose(total, highest_total, rel_tol=ROUNDING_TOLERANCE)
            )
            return tuple(offset % MONTHS_PER_YEAR + 1 for offset in range(start, start + length))
    return ()


def category_profiles(
    sku_categories: dict[str, str], sales: dict[tuple[str, date], float]
) -> dict[str, CategoryProfile]:
    totals = {category: [0.0] * MONTHS_PER_YEAR for category in sorted(set(sku_categories.values()))}
    for (sku, month), quantity in sales.items():
        if PROFILE_START_YEAR <= month.year <= PROFILE_END_YEAR:
            totals[sku_categories[sku]][month.month - 1] += quantity

    profiles = {}
    for category, monthly_totals in totals.items():
        annual_total = sum(monthly_totals)
        if annual_total > 0:
            weights = tuple(quantity / annual_total for quantity in monthly_totals)
            season_window = _season_window(monthly_totals, annual_total)
        else:
            weights = (1 / MONTHS_PER_YEAR,) * MONTHS_PER_YEAR
            season_window = ()
        profiles[category] = CategoryProfile(category, weights, season_window)
    return profiles


def is_stockout_month(opening_stock: float, sales_qty: float) -> bool:
    return opening_stock == 0 and sales_qty == 0


def estimate_sku_demand(
    sku: str,
    profile: CategoryProfile,
    history_months: tuple[date, ...],
    sales: dict[tuple[str, date], float],
    stock: dict[tuple[str, date], float],
    first_sale: date | None,
) -> SkuDemand:
    quantities = []
    weights = []
    excluded_months = 0
    for month in history_months:
        quantity = sales.get((sku, month), 0.0)
        if not is_stockout_month(stock.get((sku, month), 0.0), quantity):
            quantities.append(quantity)
            weights.append(profile.weight_for(month.month))
        elif first_sale is not None and month >= first_sale:
            excluded_months += 1

    demand_type = _demand_type(quantities)
    if not quantities:
        return SkuDemand(None, excluded_months, False, 0, demand_type)
    exposure = sum(weights)
    if exposure == 0:
        return SkuDemand(mean(quantities) * MONTHS_PER_YEAR, excluded_months, True, len(quantities), demand_type)
    return SkuDemand(sum(quantities) / exposure, excluded_months, False, len(quantities), demand_type)


def apply_analog_levels(
    sku_rows: list[dict[str, str]],
    own_demand: dict[str, SkuDemand],
    skus_with_sales: set[str],
) -> dict[str, SkuDemand]:
    groups: dict[str, tuple[str, float]] = {}
    peers: dict[tuple[str, float], dict[str, float]] = defaultdict(dict)
    for row in sku_rows:
        try:
            decile = float(row["price_decile"])
        except ValueError:
            continue
        if decile not in range(1, 11):
            continue
        sku = row["sku_id"]
        group = (row["category"], decile)
        groups[sku] = group
        annual_level = own_demand[sku].annual_level
        if annual_level is not None:
            peers[group][sku] = annual_level

    demand = own_demand.copy()
    for sku, own in own_demand.items():
        if own.eligible_months >= MIN_OWN_DEMAND_MONTHS or sku in skus_with_sales:
            continue
        sku_group = groups.get(sku)
        analog_levels = [] if sku_group is None else [
            level for peer, level in peers[sku_group].items() if peer != sku
        ]
        level = own.annual_level
        if analog_levels:
            analog_level = median(analog_levels)
            level = analog_level if level is None else min(level, analog_level)
        demand[sku] = replace(own, annual_level=level, low_data=True)
    return demand
