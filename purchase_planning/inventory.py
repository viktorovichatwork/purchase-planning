from calendar import monthrange
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from math import fsum, isclose

from purchase_planning.facts import CategoryProfile


@dataclass(frozen=True)
class StockProjection:
    stock_at_snapshot: float
    unmet_demand: float
    stockout_by_snapshot: bool


def forecast_period(
    profile: CategoryProfile, annual_level: float, start: date, end: date,
) -> Iterator[tuple[date, float]]:
    """Yield demand by month over [start, end), prorating incomplete months."""
    while start < end:
        month = start.replace(day=1)
        month_days = monthrange(month.year, month.month)[1]
        days = min(month_days - start.day + 1, (end - start).days)
        yield month, annual_level * profile.weight_for(month.month) * days / month_days
        start += timedelta(days=days)


def project_stock(
    stock: float,
    arrivals: dict[date, float],
    profile: CategoryProfile,
    annual_level: float,
    *,
    start: date,
    end: date,
    snapshot_at: date,
) -> StockProjection:
    """Project stock before receipts at snapshot_at and unmet demand over [start, end)."""
    receipt_dates = {day for day in arrivals if start <= day < end}
    snapshot = stock
    shortages = []
    stockout = False
    for day in sorted(receipt_dates | {snapshot_at, end}):
        demand = fsum(qty for _, qty in forecast_period(profile, annual_level, start, day))
        balance = 0.0 if isclose(stock, demand, rel_tol=1e-12) else stock - demand
        if day <= snapshot_at and demand > 0 and balance <= 0:
            stockout = True
        if balance < 0:
            shortages.append(-balance)
        stock = max(0.0, balance)
        if day == snapshot_at:
            snapshot = stock
        if day < end:
            stock += arrivals.get(day, 0.0)
        start = day
    return StockProjection(snapshot, fsum(shortages), stockout)
