from calendar import monthrange
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from math import ceil, floor, isclose

from purchase_planning.facts import CategoryProfile


@dataclass(frozen=True)
class StockProjection:
    stock_at_snapshot: float
    first_stockout: date | None
    lost_sales_at_snapshot: float
    stockout_days_at_snapshot: int


def shift_months(day: date, months: int) -> date:
    year, month = divmod(day.year * 12 + day.month - 1 + months, 12)
    return date(year, month + 1, min(day.day, monthrange(year, month + 1)[1]))


def month_shares(start: date, end: date) -> Iterator[tuple[date, float]]:
    """Yield the covered share of each calendar month over [start, end)."""
    while start < end:
        month = start.replace(day=1)
        month_days = monthrange(month.year, month.month)[1]
        days = min(month_days - start.day + 1, (end - start).days)
        yield month, days / month_days
        start += timedelta(days=days)


def forecast_period(
    profile: CategoryProfile, annual_level: float, start: date, end: date,
) -> Iterator[tuple[date, float]]:
    for month, share in month_shares(start, end):
        yield month, annual_level * profile.weight_for(month.month) * share


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
    """Project opening stock at snapshot_at and the first end-of-day depletion."""
    receipt_dates = {day for day in arrivals if start <= day < end}
    snapshot, lost_sales, stockout_days = stock, 0.0, 0
    first_stockout = start - timedelta(days=1) if stock == 0 else None
    for day in sorted(receipt_dates | {snapshot_at, end}):
        for month, share in month_shares(start, day):
            month_days = monthrange(month.year, month.month)[1]
            days = round(share * month_days)
            demand = annual_level * profile.weight_for(month.month) * share
            balance = 0.0 if isclose(stock, demand, rel_tol=1e-12) else stock - demand
            if day <= snapshot_at:
                lost_sales += max(0.0, -balance)
                if stock == 0:
                    stockout_days += days
                elif balance < 0:
                    stockout_days += days - floor(stock * days / demand)
            if first_stockout is None and demand > 0 and balance <= 0:
                elapsed = days if balance == 0 else ceil(stock * days / demand)
                first_stockout = start + timedelta(days=elapsed - 1)
            stock = max(0.0, balance)
            start += timedelta(days=days)
        if day == snapshot_at:
            snapshot = stock
        if day < end:
            stock += arrivals.get(day, 0.0)
        start = day
    return StockProjection(snapshot, first_stockout, lost_sales, stockout_days)
