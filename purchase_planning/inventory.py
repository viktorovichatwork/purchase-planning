from calendar import monthrange
from collections.abc import Iterator
from datetime import date, timedelta
from math import fsum, isclose

from purchase_planning.facts import CategoryProfile


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
) -> tuple[float, float]:
    """Return stock at the start of snapshot_at and total unmet demand in [start, end)."""
    receipt_dates = {day for day in arrivals if start <= day < end}
    snapshot = stock
    shortages = []
    for day in sorted(receipt_dates | {snapshot_at, end}):
        demand = fsum(qty for _, qty in forecast_period(profile, annual_level, start, day))
        balance = 0.0 if isclose(stock, demand, rel_tol=1e-12) else stock - demand
        if balance < 0:
            shortages.append(-balance)
        stock = max(0.0, balance)
        if day == snapshot_at:
            snapshot = stock
        if day < end:
            stock += arrivals.get(day, 0.0)
        start = day
    return snapshot, fsum(shortages)
