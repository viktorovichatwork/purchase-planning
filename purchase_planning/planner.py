from calendar import monthrange
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from math import ceil, fsum
from pathlib import Path
from statistics import median

from purchase_planning.calibration import read_backtest
from purchase_planning.demand import (
    DEMAND_WINDOW_MONTHS, apply_analog_levels, category_profiles, estimate_sku_demand,
)
from purchase_planning.export import ExportValidationError
from purchase_planning.facts import (
    NO_SUPPLIER_FLAG, TYPICAL_TIMING_FLAG, TYPICAL_TIMING_SOURCE, DecisionReason, DemandType, OrderFlag,
    PlanFacts, Recommendation, Segment, SupplierSummary, SupplierTiming,
)
from purchase_planning.inventory import forecast_period, project_stock
from purchase_planning.orders import displayed_order_qty
from purchase_planning.prepared_export import PreparedExport, prepare_export
from purchase_planning.replenishment import InventoryPlan, SeasonalBatch, matching_seasonal_batch, plan_inventory
from purchase_planning.segments import Sale, classify_sku, in_assortment_window, month_index
from purchase_planning.supply import PurchaseLine, Receipt, classify_transit, read_supply

STOCK_HISTORY_START = date(2024, 1, 1)


@dataclass(frozen=True)
class Calculation:
    facts: PlanFacts
    sales: dict[str, list[Sale]]
    receipts: list[Receipt]
    orders: list[PurchaseLine]


def _typical_timing(timings: Iterable[SupplierTiming]) -> SupplierTiming | None:
    known = [terms for terms in timings if terms.horizon_days is not None]
    if not known:
        return None
    return SupplierTiming(
        ceil(median(terms.lead_days for terms in known)), TYPICAL_TIMING_SOURCE,
        ceil(median(terms.cycle_days for terms in known)),
    )


def _complete_stock_months(as_of: date, partial_months: set[date]) -> tuple[date, ...]:
    months = []
    for year in range(STOCK_HISTORY_START.year, as_of.year + 1):
        for month in range(1, 13):
            month_end = date(year, month, monthrange(year, month)[1])
            if month_end <= as_of and month_end not in partial_months:
                months.append(month_end)
    return tuple(months)


def _sku_known_at(row: dict[str, str], as_of: date) -> bool:
    try:
        created = date.fromisoformat(f"{row['created']}-01")
    except ValueError:
        # Older exports can lack a creation month.
        return True
    return created <= as_of


def plan(data_dir: str | Path, as_of: date | None = None) -> PlanFacts:
    return calculate(data_dir, as_of).facts


def calculate(
    data_dir: str | Path, as_of: date | None = None, *,
    backtest_dir: Path | None = None,
) -> Calculation:
    return calculate_export(prepare_export(data_dir), as_of, backtest_dir=backtest_dir)


def calculate_export(
    export: PreparedExport, as_of: date | None = None, *, backtest_dir: Path | None = None,
) -> Calculation:
    """Calculate one date from a prepared export without rereading the ERP files."""
    directory, tables = export.directory, export.tables
    sales_rows, stock, partial_months = export.sales_rows, export.stock, export.partial_months
    order_history, receipt_history = export.orders, export.receipts
    cutoff, cutoff_source = export.cutoff, export.cutoff_source
    calculation_date = cutoff if as_of is None else as_of
    if calculation_date > cutoff:
        raise ExportValidationError(["Дата расчёта позже даты среза."])
    backtest, backtest_warning = read_backtest(backtest_dir or directory, calculation_date)

    sku_rows = tables["sku_dim"]
    if calculation_date < cutoff:
        known_skus = {sku for sku, sale in sales_rows if sale.month <= calculation_date}
        sku_rows = [row for row in sku_rows if row["sku_id"] in known_skus or _sku_known_at(row, calculation_date)]
    sku_categories = {row["sku_id"]: row["category"] for row in sku_rows}
    stock_month = calculation_date.replace(day=monthrange(calculation_date.year, calculation_date.month)[1])
    month_start = calculation_date.replace(day=1)
    forecast_start = calculation_date + timedelta(days=1)
    know_month_sales = calculation_date == cutoff or (
        calculation_date == stock_month and stock_month not in partial_months
    )
    net_sales: dict[tuple[str, date], float] = defaultdict(float)
    gross_sales: dict[tuple[str, date], float] = defaultdict(float)
    month_sales: dict[str, float] = defaultdict(float)
    first_sales: dict[str, date] = {}
    sales_by_sku: dict[str, list[Sale]] = defaultdict(list)
    for sku, sale in sales_rows:
        if sku not in sku_categories:
            continue
        month, quantity = sale.month, sale.quantity
        if month <= calculation_date and month not in partial_months:
            net_sales[sku, month] += sale.net_quantity
            gross_sales[sku, month] += quantity
        if month == stock_month and know_month_sales:
            month_sales[sku] += quantity
        # A sale through the export cutoff is known even in an incomplete month.
        if month <= calculation_date or (month == stock_month and know_month_sales):
            sales_by_sku[sku].append(replace(sale, partial=True) if month in partial_months else sale)
            if quantity > 0:
                first_sales[sku] = min(first_sales.get(sku, month), month)

    profiles = category_profiles(sku_categories, net_sales)
    history_months = _complete_stock_months(calculation_date, partial_months)[-DEMAND_WINDOW_MONTHS:]
    own_demand = {
        sku: estimate_sku_demand(
            sku, profiles[category], history_months, net_sales, gross_sales, stock, first_sales.get(sku),
        )
        for sku, category in sku_categories.items()
    }
    demand_by_sku = apply_analog_levels(sku_rows, own_demand, set(first_sales))

    timing, receipts, orders, order_multiples = read_supply(tables, calculation_date, cutoff, order_history, receipt_history)
    open_lines = [line for line in orders if line.status == "open"]
    open_skus = {line.sku for line in open_lines}
    transit, order_flags = classify_transit(open_lines, calculation_date)
    open_orders: dict[str, list[tuple[date | None, float]]] = defaultdict(list)
    for line in open_lines:
        if line.pending > 0:
            # A missing or impossible arrival date (before the order date) is shown as missing.
            valid = line.promised is not None and line.promised >= line.placed
            open_orders[line.sku].append((line.promised if valid else None, line.pending))
    month_receipts: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    current_month = month_index(calculation_date)
    last_complete_month = current_month - (calculation_date < stock_month or stock_month in partial_months)
    window = {"as_of": calculation_date, "last_complete_month": last_complete_month}
    recent_stock = {
        sku for (sku, month), quantity in stock.items() if quantity > 0 and in_assortment_window(month, **window)
    }
    for receipt in receipts:
        if in_assortment_window(receipt.received, **window):
            recent_stock.add(receipt.sku)
        if month_start <= receipt.received:
            month_receipts[receipt.sku][receipt.received] += receipt.qty

    typical_timing = _typical_timing(timing.values())
    typical_skus: set[str] = set()
    sku_plans: list[tuple[Recommendation, dict[date, float]]] = []
    for sku_row in sorted(sku_rows, key=lambda row: (row["supplier_id"], row["sku_id"])):
        sku = sku_row["sku_id"]
        supplier = sku_row["supplier_id"]
        profile = profiles[sku_row["category"]]
        demand = demand_by_sku[sku]
        annual_level = max(0.0, demand.annual_level or 0.0)
        opening_stock = stock.get((sku, stock_month), 0.0)
        if know_month_sales:
            current_stock = max(0.0, opening_stock + fsum(month_receipts[sku].values()) - month_sales[sku])
        else:
            current_stock = project_stock(
                opening_stock, month_receipts[sku], profile, annual_level,
                start=month_start, end=forecast_start, snapshot_at=forecast_start,
            ).stock_at_snapshot

        arrivals = transit.get(sku, {})
        monthly_transit: dict[date, float] = defaultdict(float)
        for day, quantity in sorted(arrivals.items()):
            monthly_transit[day.replace(day=1)] += quantity
        supplier_timing = timing[supplier]
        assortment = classify_sku(
            sales_by_sku[sku], first_sales.get(sku), as_of=calculation_date,
            last_complete_month=last_complete_month, had_recent_stock=sku in recent_stock or current_stock > 0,
            low_data=demand.low_data,
        )
        segment = assortment.segment
        decision = assortment.decision
        reasons = list(decision.reasons)
        flags = list(assortment.flags)
        unknown_supplier = not supplier or sku_row["supply_type"] == "unknown"
        if unknown_supplier:
            flags.append(NO_SUPPLIER_FLAG)
            reasons.append(DecisionReason.MISSING_SUPPLIER)
        selling = segment in {Segment.PURCHASE, Segment.CONDITIONAL} and assortment.sales_qty_12m > 0
        unknown_timing = unknown_supplier or supplier_timing.horizon_days is None
        if selling and unknown_timing and typical_timing is not None:
            # The buyer has to name a supplier. Until then the order is sized on a typical supplier.
            supplier_timing, unknown_supplier = typical_timing, False
            typical_skus.add(sku)
            reasons.append(DecisionReason.TYPICAL_TIMING)
            decision = replace(decision, typical_timing=typical_timing)
            segment = Segment.CONDITIONAL
            flags.append(f"{TYPICAL_TIMING_FLAG} {typical_timing.lead_days} дн.")
        lead, cycle = supplier_timing.lead_days, supplier_timing.cycle_days
        if lead is None:
            flags.append("Не определено плечо поставки")
        if cycle is None:
            flags.append("Нет истории для цикла")
        if demand.long_stockout:
            reasons.append(DecisionReason.LONG_STOCKOUT)
            if selling:
                segment = Segment.CONDITIONAL
        try:
            decile = float(sku_row["price_decile"])
            price_decile = int(decile) if decile in range(1, 11) else None
        except ValueError:
            price_decile = None
        inventory = InventoryPlan(calculation_date, current_stock, arrivals, profile, annual_level)
        forecast_end = forecast_start + timedelta(days=supplier_timing.horizon_days or lead or 0)
        forecast = dict(forecast_period(profile, annual_level, forecast_start, forecast_end))
        recommendation = plan_inventory(
            Recommendation(
                sku_id=sku, supplier_id=supplier, order_qty=0.0,
                excluded_stockout_months=demand.excluded_stockout_months, low_data=demand.low_data,
                missing_stock_months=demand.missing_stock_months, checked_stock_months=demand.checked_stock_months,
                seasonal_category=profile.is_seasonal,
                stock_at_calculation=current_stock, in_transit_by_month=tuple(monthly_transit.items()),
                open_orders=tuple(sorted(open_orders[sku], key=lambda order: (order[0] or date.min, order[1]))),
                order_flags=tuple(sorted(order_flags.get(sku, ()))), segment=segment, is_new=assortment.is_new,
                markup_pct_12m=assortment.markup_pct_12m, sales_qty_12m=assortment.sales_qty_12m,
                sku_flags=tuple(flags), annual_demand_qty=annual_level, category=profile.category,
                decision=replace(decision, reasons=tuple(reasons)),
                demand_type=demand.demand_type, order_multiple=order_multiples.get(sku, 1), price_decile=price_decile,
            ),
            inventory, supplier_timing, backtest, known_supplier=not unknown_supplier, has_open_orders=sku in open_skus,
        )
        sku_plans.append((recommendation, forecast))

    season_batches: dict[str, set[SeasonalBatch]] = defaultdict(set)
    for row, _ in sku_plans:
        if row.seasonal_order and row.seasonal_order.actionable:
            deadline = row.seasonal_order.deadline
            season_batches[row.supplier_id].add(SeasonalBatch(max(calculation_date, deadline), deadline))
    quantities_by_supplier: dict[str, list[float]] = defaultdict(list)
    dates_by_supplier: dict[str, list[date]] = defaultdict(list)
    for index, (row, forecast) in enumerate(sku_plans):
        if (
            row.demand_type == DemandType.REGULAR and not profiles[row.category].is_seasonal
            and row.sku_id not in typical_skus
        ):
            batch = matching_seasonal_batch(row, season_batches[row.supplier_id], calculation_date)
            if batch is not None:
                # Recompute against the stock on the grouped batch's arrival date.
                base = replace(row, order_flags=tuple(flag for flag in row.order_flags if flag != OrderFlag.RESCHEDULE))
                row = plan_inventory(
                    base, InventoryPlan(calculation_date, row.stock_at_calculation, transit.get(row.sku_id, {}),
                                        profiles[row.category], row.annual_demand_qty),
                    timing[row.supplier_id], backtest, known_supplier=True,
                    has_open_orders=row.sku_id in open_skus, batch=batch,
                )
                sku_plans[index] = row, forecast
        if row.segment == Segment.PURCHASE:
            quantities_by_supplier[row.supplier_id].append(displayed_order_qty(row))
        if row.order_qty > 0 and row.order_by is not None:
            dates_by_supplier[row.supplier_id].append(row.order_by)

    forecast_months = tuple(sorted({month for _, forecast in sku_plans for month in forecast}))
    suppliers = []
    for supplier, terms in timing.items():
        supplier_deadline = min(dates_by_supplier[supplier], default=None)
        # Nobody can place an order without a supplier: the buyer names one first.
        due = bool(supplier) and supplier_deadline is not None and (
            supplier_deadline <= calculation_date + timedelta(days=terms.cycle_days or 0)
        )
        suppliers.append(SupplierSummary(supplier, terms, fsum(quantities_by_supplier[supplier]), due, supplier_deadline))
    supply_warning = None
    if calculation_date < cutoff:
        supply_warning = (
            "Исторический расчёт: статусы заказов на дату расчёта неизвестны, «в пути» пропущено. "
            "Если продажи до даты расчёта неизвестны, остаток оценён по прогнозу."
        )
    facts = PlanFacts(
        cutoff, calculation_date, tuple(
            replace(row, monthly_forecast=tuple(forecast.get(month, 0.0) for month in forecast_months))
            for row, forecast in sku_plans
        ), cutoff_source, forecast_months, tuple(profiles.values()), tuple(suppliers), supply_warning, backtest,
        backtest_warning, unconfirmed_order_lines=sum(line.is_unconfirmed for line in open_lines),
        typical_timing=typical_timing,
    )
    return Calculation(facts, sales_by_sku, receipts, orders)
