from dataclasses import dataclass, replace
from datetime import date, timedelta
from math import fsum, isclose

from purchase_planning.calibration import stock_multiplier
from purchase_planning.facts import (
    BacktestResult, CategoryProfile, OrderFlag, PlanFacts, Recommendation, SeasonalOrder, SeasonStatus, Segment,
    SupplierTiming,
)
from purchase_planning.inventory import forecast_period, month_shares, project_stock, shift_months
from purchase_planning.order_sheet import rounded_quantity

EXCESS_MONTHS = 12
SLOW_COVERAGE_MONTHS = 6
SEASON_GROUPING_MONTHS = 1
DAY = timedelta(days=1)


@dataclass(frozen=True)
class Replenishment:
    stock_at_arrival: float
    target_stock: float
    safety_stock: float

    @property
    def quantity(self) -> float:
        return max(0.0, self.target_stock - self.stock_at_arrival)


@dataclass(frozen=True, order=True)
class SeasonalBatch:
    placement: date
    deadline: date


@dataclass(frozen=True)
class InventoryPlan:
    as_of: date
    stock: float
    arrivals: dict[date, float]
    profile: CategoryProfile
    annual_demand: float

    def demand(self, start: date, end: date, *, outside_season: bool = False) -> float:
        return fsum(
            qty for month, qty in forecast_period(self.profile, self.annual_demand, start, end)
            if not outside_season or month.month not in self.profile.season_window
        )

    def stock_before(self, day: date) -> float:
        return project_stock(
            self.stock, self.arrivals, self.profile, self.annual_demand,
            start=self.as_of + DAY, end=day, snapshot_at=day,
        ).stock_at_snapshot

    def regular_order(
        self, placed: date, lead: int, cycle: int, multiplier: float, stock_at_arrival: float,
    ) -> Replenishment:
        """Order for the cycle after arrival, sized for sales running `multiplier` times the forecast.

        Under that stress scenario the stock left at arrival shrinks by the extra sales before
        arrival, but never below zero: sales lost before arrival need no safety stock.
        """
        arrival = placed + timedelta(days=lead)
        start = arrival + DAY
        end = start + timedelta(days=cycle)
        if self.profile.is_seasonal:
            window_start, window_end = self.season_window(start)
            end = start if start >= window_start else min(end, window_start)
            if window_start <= arrival < window_end or end <= start:
                return Replenishment(stock_at_arrival, 0.0, 0.0)
        extra = multiplier - 1
        extra_before_arrival = extra * self.demand(placed + DAY, start, outside_season=True)
        stressed_stock = max(0.0, stock_at_arrival - extra_before_arrival)
        safety = extra * self.demand(start, end, outside_season=True) + stock_at_arrival - stressed_stock
        return Replenishment(stock_at_arrival, self.demand(start, end) + safety, safety)

    def stressed_stockout(self, multiplier: float, end: date) -> date | None:
        """First day without stock if sales outside the season run `multiplier` times the forecast."""
        season = self.profile.season_window
        weights = tuple(
            weight if month in season else weight * multiplier
            for month, weight in enumerate(self.profile.weights, start=1)
        )
        return project_stock(
            self.stock, self.arrivals, replace(self.profile, weights=weights), self.annual_demand,
            start=self.as_of + DAY, end=end, snapshot_at=end,
        ).first_stockout

    def season_window(self, day: date) -> tuple[date, date]:
        months = self.profile.season_window
        start = date(day.year - 1, months[0], 1)
        end = shift_months(start, len(months))
        while end <= day:
            start, end = shift_months(start, 12), shift_months(end, 12)
        return start, end

    def seasonal_order(
        self, arrival_today: date, lead: int, multiple: int, seasonal_multiplier: float, ordinary_multiplier: float,
        *, regular_receipt: tuple[date, int] | None = None,
    ) -> SeasonalOrder:
        start, end = self.season_window(self.as_of)
        deadline = start - timedelta(days=lead)
        next_deadline = shift_months(start, 12) - timedelta(days=lead)
        days_late = max(0, (self.as_of - deadline).days)
        # On time, the batch is available for the whole opening day. A late batch
        # follows the usual convention of stock measured at the end of arrival day.
        available = arrival_today + DAY if days_late else start
        remaining = self.demand(available, end)
        months_remaining = fsum(share for _, share in month_shares(available, end))
        arrivals = self.arrivals.copy()
        if regular_receipt is not None:
            day, qty = regular_receipt
            arrivals[day] = arrivals.get(day, 0.0) + qty
        stock = replace(self, arrivals=arrivals).stock_before(min(available, end))
        if not days_late:
            stock += arrivals.get(start, 0.0)
        target = remaining * seasonal_multiplier
        quantity = max(0.0, target - stock)
        ordinary_qty = max(0.0, remaining * ordinary_multiplier - stock)
        if days_late and arrival_today >= end:
            status = SeasonStatus.MISSED
        elif days_late:
            peak_month = self.annual_demand * max(self.profile.weight_for(month) for month in self.profile.season_window)
            below_peak_month = quantity < peak_month and not isclose(quantity, peak_month, rel_tol=1e-12)
            below_multiple = quantity < multiple and not isclose(quantity, multiple, rel_tol=1e-12)
            status = SeasonStatus.NOT_RECOMMENDED if below_peak_month or below_multiple else SeasonStatus.LATE
        else:
            status = SeasonStatus.ON_TIME

        if quantity > 0 and status in {SeasonStatus.ON_TIME, SeasonStatus.LATE}:
            arrivals[available] = arrivals.get(available, 0.0) + rounded_quantity(quantity, multiple)
        projected = replace(self, arrivals=arrivals).stock_before(end)
        return SeasonalOrder(
            window_start=start, window_end=end - DAY, deadline=deadline, days_late=days_late,
            months_remaining=months_remaining, order_qty=quantity, ordinary_service_qty=ordinary_qty,
            stock_at_season_end=projected, next_deadline=next_deadline, status=status,
            stock_at_arrival=stock, target_stock=target, safety_stock=target - remaining,
        )


def matching_seasonal_batch(
    row: Recommendation, batches: set[SeasonalBatch], as_of: date,
) -> SeasonalBatch | None:
    if row.regular_order_date is None or not row.regular_order_qty:
        return None
    own_date = max(as_of, row.regular_order_date)
    matches = [
        batch for batch in batches
        if shift_months(batch.placement, -SEASON_GROUPING_MONTHS) <= own_date
        <= shift_months(batch.placement, SEASON_GROUPING_MONTHS)
    ]
    return min(matches, key=lambda batch: (abs(batch.placement - own_date), batch), default=None)


def plan_inventory(
    row: Recommendation, inventory: InventoryPlan, timing: SupplierTiming, backtest: BacktestResult | None,
    *, known_supplier: bool, has_open_orders: bool, batch: SeasonalBatch | None = None,
) -> Recommendation:
    as_of = inventory.as_of
    start = as_of + DAY
    lead, cycle = timing.lead_days, timing.cycle_days
    annual_end = shift_months(start, EXCESS_MONTHS)
    end = max(annual_end, start + timedelta(days=timing.horizon_days or lead or 0))
    cycle_start = start + timedelta(days=lead or 0)
    projection = project_stock(
        inventory.stock, inventory.arrivals, inventory.profile, inventory.annual_demand,
        start=start, end=end, snapshot_at=cycle_start,
    )
    purchase_segment = row.segment in {Segment.PURCHASE, Segment.CONDITIONAL}
    can_order = purchase_segment and known_supplier
    stock_until = projection.first_stockout if purchase_segment else None
    position = inventory.stock + fsum(inventory.arrivals.values())
    depletion = projection.first_stockout
    coverage_end = depletion + DAY if depletion is not None else end
    coverage = fsum(share for _, share in month_shares(start, coverage_end)) if inventory.annual_demand > 0 else None

    threshold = inventory.demand(start, annual_end)
    season_start, season_end = inventory.season_window(as_of) if inventory.profile.is_seasonal else (None, None)
    if season_end is not None:
        threshold = max(threshold, inventory.demand(start, season_end))

    multiple = row.order_multiple
    arrival = as_of + timedelta(days=lead) if lead is not None else None
    ordinary_multiplier = stock_multiplier(backtest, row.demand_type, False)
    regular: Replenishment | None = None
    regular_deadline: date | None = None
    regular_receipt: tuple[date, int] | None = None
    regular_qty = 0.0
    if lead is not None and cycle is not None:
        regular = inventory.regular_order(
            as_of, lead, cycle, ordinary_multiplier, projection.stock_at_snapshot,
        )
        threshold = max(threshold, multiple + inventory.demand(start, cycle_start) + regular.target_stock)
        if can_order:
            placed = as_of
            if stock_until is not None:
                # The order has to arrive before the stock runs out even if sales run faster than forecast.
                stressed_until = inventory.stressed_stockout(ordinary_multiplier, end) or stock_until
                regular_deadline = stressed_until - timedelta(days=lead)
                placed = max(as_of, regular_deadline)
            if batch is not None:
                placed = batch.placement
            snapshot = placed + timedelta(days=lead + 1)
            if placed > as_of:
                regular = inventory.regular_order(placed, lead, cycle, ordinary_multiplier, inventory.stock_before(snapshot))
            regular_qty = regular.quantity
            if regular_qty > 0:
                threshold = max(threshold, multiple + inventory.demand(start, snapshot) + regular.target_stock)
                if season_start is not None and snapshot <= season_start:
                    regular_receipt = snapshot, rounded_quantity(regular_qty, multiple)

    seasonal_order = None
    segment = row.segment
    sku_flags = row.sku_flags
    if season_start is not None and season_end is not None and lead is not None and arrival is not None:
        seasonal_multiplier = stock_multiplier(backtest, row.demand_type, True)
        season = inventory.seasonal_order(
            arrival, lead, multiple, seasonal_multiplier, ordinary_multiplier, regular_receipt=regular_receipt,
        )
        # An open batch can still serve the full window after the deadline for
        # placing a new order has passed. Use the original seasonal target.
        seasonal_coverage = inventory.demand(season_start, season_end) * seasonal_multiplier
        threshold = max(threshold, multiple + inventory.demand(start, season_start) + seasonal_coverage)
        if can_order:
            seasonal_order = season
            if season.status in {SeasonStatus.LATE, SeasonStatus.NOT_RECOMMENDED}:
                segment = Segment.CONDITIONAL
            if season.expedite:
                sku_flags += ("Рассмотреть ускоренную доставку",)
            if not regular_qty and cycle is not None and season_start <= cycle_start <= season_end < (
                cycle_start + timedelta(days=cycle)
            ):
                regular_deadline = season_end - timedelta(days=lead + 1)
                regular = inventory.regular_order(
                    regular_deadline, lead, cycle, ordinary_multiplier, season.stock_at_season_end,
                )
                regular_qty = regular.quantity
                threshold = max(threshold, multiple + inventory.demand(start, season_end) + regular.target_stock)

    seasonal_qty = seasonal_order.order_qty if seasonal_order and seasonal_order.actionable else 0.0
    deadlines = [regular_deadline] if regular_qty and regular_deadline is not None else []
    if seasonal_qty and seasonal_order is not None:
        deadlines.append(seasonal_order.deadline)
    order_by = min(deadlines, default=regular_deadline)
    if not regular_qty and seasonal_order is not None:
        order_by = seasonal_order.deadline
    if batch is not None:
        order_by = batch.placement
    target = seasonal_order if seasonal_order and not regular_qty else regular
    stockout_risk = bool(
        row.sales_qty_12m > 0 and stock_until is not None and arrival is not None and stock_until < arrival
    )
    excess = row.sales_qty_12m > 0 and position > threshold and not isclose(position, threshold, rel_tol=1e-12)
    order_flags = row.order_flags
    if excess and has_open_orders:
        order_flags = tuple(sorted((*order_flags, OrderFlag.RESCHEDULE)))

    return replace(
        row, order_qty=regular_qty + seasonal_qty, order_flags=order_flags, sku_flags=sku_flags, segment=segment,
        arrival_date=arrival, stock_at_arrival=projection.stock_at_snapshot if arrival is not None else None,
        target_stock=target.target_stock if target else None, safety_stock=target.safety_stock if target else None,
        coverage_months=coverage, stock_until=depletion if coverage is not None else stock_until, order_by=order_by,
        stockout_risk=stockout_risk, stockout_days=projection.stockout_days_at_snapshot if stockout_risk else 0,
        lost_sales=projection.lost_sales_at_snapshot if stockout_risk else 0.0,
        excess_threshold=threshold, excess=excess, seasonal_order=seasonal_order,
        regular_order_qty=regular_qty, regular_order_date=regular_deadline,
        grouped_season_deadline=batch.deadline if batch is not None else None,
    )


def inventory_summary(facts: PlanFacts) -> dict[str, int]:
    rows = facts.recommendations
    return {
        "Покрытие 6–12 мес": sum(
            row.stock_until is not None and row.coverage_months is not None
            and SLOW_COVERAGE_MONTHS <= row.coverage_months <= EXCESS_MONTHS
            for row in rows
        ),
        "Избыток": sum(row.excess for row in rows),
        "Риски дефицита": sum(row.stockout_risk for row in rows),
    }
