import json
from calendar import monthrange
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, timedelta
from math import fsum
from pathlib import Path

from purchase_planning.demand import apply_analog_levels, category_profiles, estimate_sku_demand
from purchase_planning.export import ExportValidationError, finite_quantity, read_export
from purchase_planning.facts import PlanFacts, Recommendation, Segment, SupplierSummary
from purchase_planning.inventory import forecast_period, project_stock
from purchase_planning.segments import ASSORTMENT_MONTHS, Sale, classify_sku, month_index
from purchase_planning.supply import PurchaseLine, Receipt, classify_transit, read_supply

STOCK_HISTORY_START = date(2024, 1, 1)


@dataclass(frozen=True)
class Calculation:
    facts: PlanFacts
    sales: dict[str, list[Sale]]
    receipts: list[Receipt]
    orders: list[PurchaseLine]


def _month_end(month: str) -> date:
    first = date.fromisoformat(f"{month}-01")
    return first.replace(day=monthrange(first.year, first.month)[1])


def _complete_stock_months(as_of: date, partial_months: set[date]) -> tuple[date, ...]:
    months = []
    for year in range(STOCK_HISTORY_START.year, as_of.year + 1):
        for month in range(1, 13):
            month_end = date(year, month, monthrange(year, month)[1])
            if month_end <= as_of and month_end not in partial_months:
                months.append(month_end)
    return tuple(months)


def _read_cutoff(path: Path, complete_months: list[date]) -> tuple[date, str | None]:
    try:
        contents = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        if not complete_months:
            raise ExportValidationError(
                ["Нет export_meta.json и в продажах нет полного месяца для определения даты среза."]
            ) from None
        return max(complete_months), (
            "Нет export_meta.json. Дата среза принята равной последнему дню последнего полного месяца."
        )
    except (OSError, UnicodeError):
        raise ExportValidationError(["Не удалось прочитать export_meta.json в UTF-8."]) from None

    try:
        value = json.loads(contents)["cutoff_date"]
        cutoff = date.fromisoformat(value)
        if cutoff.isoformat() != value:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ExportValidationError(
            ["export_meta.json: укажите cutoff_date в формате YYYY-MM-DD."]
        ) from None
    return cutoff, None


def plan(
    data_dir: str | Path, as_of: date | None = None, *,
    previous_segments: Mapping[str, Segment] | None = None,
) -> PlanFacts:
    return calculate(data_dir, as_of, previous_segments=previous_segments).facts


def calculate(
    data_dir: str | Path, as_of: date | None = None, *,
    previous_segments: Mapping[str, Segment] | None = None,
) -> Calculation:
    directory = Path(data_dir)
    tables = read_export(directory)
    reasons: list[str] = []
    sales_rows: list[tuple[str, Sale]] = []
    partial_months: set[date] = set()
    for row in tables["sales_monthly"]:
        try:
            month_end = _month_end(row["month"])
            quantity = finite_quantity(row["qty_sold"])
            returns = finite_quantity(row["qty_returned"].strip() or "0")
            if row["partial_month"] not in {"0", "1"}:
                raise ValueError
        except ValueError:
            reasons.append(
                f"sales_monthly.csv, SKU {row['sku_id']}: проверьте month (YYYY-MM), "
                "qty_sold и qty_returned (числа), partial_month (0 или 1)."
            )
            continue
        try:
            markup = finite_quantity(row["markup_pct"])
        except ValueError:
            markup = None
        partial = row["partial_month"] == "1"
        sales_rows.append((row["sku_id"], Sale(month_end, quantity, returns, markup, partial)))
        if partial:
            partial_months.add(month_end)

    stock: dict[tuple[str, date], float] = {}
    for row in tables["stock_monthly"]:
        try:
            stock[row["sku_id"], _month_end(row["month"])] = finite_quantity(row["stock_qty"])
        except ValueError:
            reasons.append(
                f"stock_monthly.csv, SKU {row['sku_id']}: проверьте month (YYYY-MM) и stock_qty (число)."
            )
    if reasons:
        raise ExportValidationError(reasons)

    cutoff, warning = _read_cutoff(
        directory / "export_meta.json",
        [sale.month for _, sale in sales_rows if sale.month not in partial_months],
    )
    calculation_date = cutoff if as_of is None else as_of
    if calculation_date > cutoff:
        raise ExportValidationError(["Дата расчёта позже даты среза."])

    stock_month = _month_end(calculation_date.strftime("%Y-%m"))
    month_start = calculation_date.replace(day=1)
    forecast_start = calculation_date + timedelta(days=1)
    know_month_sales = calculation_date == cutoff or (
        calculation_date == stock_month and stock_month not in partial_months
    )
    sales: dict[tuple[str, date], float] = defaultdict(float)
    month_sales: dict[str, float] = defaultdict(float)
    first_sales: dict[str, date] = {}
    sales_by_sku: dict[str, list[Sale]] = defaultdict(list)
    for sku, sale in sales_rows:
        month, quantity = sale.month, sale.quantity
        if month <= calculation_date and month not in partial_months:
            sales[sku, month] += quantity
        if month == stock_month and know_month_sales:
            month_sales[sku] += quantity
        # A sale through the export cutoff is known even in an incomplete month.
        if month <= calculation_date or (month == stock_month and know_month_sales):
            sales_by_sku[sku].append(replace(sale, partial=True) if month in partial_months else sale)
            if quantity > 0:
                first_sales[sku] = min(first_sales.get(sku, month), month)

    sku_rows = tables["sku_dim"]
    sku_categories = {row["sku_id"]: row["category"] for row in sku_rows}
    profiles = category_profiles(sku_categories, sales)
    history_months = _complete_stock_months(calculation_date, partial_months)
    own_demand = {
        sku: estimate_sku_demand(sku, profiles[category], history_months, sales, stock, first_sales.get(sku))
        for sku, category in sku_categories.items()
    }
    demand_by_sku = apply_analog_levels(sku_rows, own_demand, set(first_sales))

    timing, receipts, orders = read_supply(tables, calculation_date, cutoff)
    transit, order_flags = classify_transit([line for line in orders if line.status == "open"], calculation_date)
    month_receipts: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    current_month = month_index(calculation_date)
    last_complete_month = current_month - (calculation_date < stock_month or stock_month in partial_months)
    recent_stock = {
        sku for (sku, month), quantity in stock.items()
        if quantity > 0 and current_month - ASSORTMENT_MONTHS < month_index(month) <= current_month
    }
    for receipt in receipts:
        if month_index(receipt.received) > current_month - ASSORTMENT_MONTHS:
            recent_stock.add(receipt.sku)
        if month_start <= receipt.received:
            month_receipts[receipt.sku][receipt.received] += receipt.qty

    sku_plans: list[tuple[Recommendation, dict[date, float]]] = []
    quantities_by_supplier: dict[str, list[float]] = defaultdict(list)
    suppliers_due = set()
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
        lead, cycle = supplier_timing.lead_days, supplier_timing.cycle_days
        assortment = classify_sku(
            sales_by_sku[sku], first_sales.get(sku), as_of=calculation_date,
            last_complete_month=last_complete_month, had_recent_stock=sku in recent_stock or current_stock > 0,
            low_data=demand.low_data,
            previous_segment=previous_segments.get(sku) if previous_segments is not None else None,
        )
        flags = list(assortment.flags)
        unknown_supplier = not supplier or sku_row["supply_type"] == "unknown"
        if unknown_supplier:
            flags.append("Поставщик не указан")
        if lead is None:
            flags.append("Не определено плечо поставки")
        if cycle is None:
            flags.append("Нет истории для цикла")
        forecast = {}
        arrival: date | None = None
        arrival_stock: float | None = None
        order_qty = 0.0
        stockout_risk = False
        purchase_segment = assortment.segment in {Segment.PURCHASE, Segment.CONDITIONAL}
        if lead is not None:
            arrival = calculation_date + timedelta(days=lead)
            horizon_end = forecast_start + timedelta(days=lead + (cycle or 0))
            forecast = dict(forecast_period(profile, annual_level, forecast_start, horizon_end))
            # Both the cutoff and the expected arrival are measured at the end of the day.
            projection = project_stock(
                current_stock, arrivals, profile, annual_level,
                start=forecast_start, end=horizon_end, snapshot_at=arrival + timedelta(days=1),
            )
            arrival_stock = projection.stock_at_snapshot
            stockout_risk = (
                purchase_segment
                and sku in first_sales and assortment.sales_qty_12m > 0
                and lead > 0 and projection.stockout_by_snapshot
            )
            if cycle is not None and not unknown_supplier and purchase_segment:
                order_qty = projection.unmet_demand
        recommendation = Recommendation(
            sku_id=sku, supplier_id=supplier, order_qty=order_qty,
            excluded_stockout_months=demand.excluded_stockout_months, low_data=demand.low_data,
            stock_at_calculation=current_stock, arrival_date=arrival, stock_at_arrival=arrival_stock,
            in_transit_by_month=tuple(monthly_transit.items()),
            order_flags=tuple(sorted(order_flags.get(sku, set()))),
            segment=assortment.segment, is_new=assortment.is_new,
            markup_pct_12m=assortment.markup_pct_12m, sales_qty_12m=assortment.sales_qty_12m,
            sku_flags=tuple(flags), stockout_risk=stockout_risk, annual_demand_qty=annual_level,
        )
        sku_plans.append((recommendation, forecast))
        if assortment.segment == Segment.PURCHASE:
            quantities_by_supplier[supplier].append(order_qty)
            if order_qty > 0 and sku in first_sales:
                suppliers_due.add(supplier)

    forecast_months = tuple(sorted({month for _, forecast in sku_plans for month in forecast}))
    suppliers = tuple(
        SupplierSummary(supplier, terms, fsum(quantities_by_supplier[supplier]), supplier in suppliers_due)
        for supplier, terms in timing.items()
    )
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
        ), warning, forecast_months, tuple(profiles.values()), suppliers, supply_warning,
    )
    return Calculation(facts, sales_by_sku, receipts, orders)
