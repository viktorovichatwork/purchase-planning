from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from math import isclose
from statistics import mean, median

from purchase_planning.facts import CategoryProfile

MONTHS_PER_YEAR = 12
PROFILE_START_YEAR = 2020
PROFILE_END_YEAR = 2025
ROUNDING_TOLERANCE = 1e-15


@dataclass(frozen=True)
class SkuDemand:
    annual_level: float | None
    excluded_stockout_months: int
    low_data: bool


def _season_window(monthly_totals: list[float], annual_total: float) -> tuple[int, ...]:
    # Doubling the calendar lets consecutive windows cross December.
    calendar = monthly_totals * 2
    starts = range(MONTHS_PER_YEAR)
    peak = max(sum(calendar[start:start + 3]) for start in starts)
    half = annual_total * 0.5
    if peak < half and not isclose(peak, half, rel_tol=ROUNDING_TOLERANCE):
        return ()

    threshold = annual_total * 0.8
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


def estimate_sku_demand(
    sku: str,
    profile: CategoryProfile,
    history_months: tuple[date, ...],
    sales: dict[tuple[str, date], float],
    stock: dict[tuple[str, date], float],
) -> SkuDemand:
    quantities = []
    weights = []
    for month in history_months:
        quantity = sales.get((sku, month), 0.0)
        if stock.get((sku, month), 0.0) != 0 or quantity != 0:
            quantities.append(quantity)
            weights.append(profile.weight_for(month.month))

    excluded_months = len(history_months) - len(quantities)
    if not quantities:
        return SkuDemand(None, excluded_months, False)
    exposure = sum(weights)
    if exposure == 0:
        return SkuDemand(mean(quantities) * MONTHS_PER_YEAR, excluded_months, True)
    return SkuDemand(sum(quantities) / exposure, excluded_months, False)


def apply_analog_levels(
    sku_rows: list[dict[str, str]],
    own_demand: dict[str, SkuDemand],
    history_length: int,
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
        eligible_months = history_length - own.excluded_stockout_months
        if eligible_months >= 3 or sku in skus_with_sales:
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
