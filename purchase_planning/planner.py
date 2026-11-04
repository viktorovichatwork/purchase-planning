import json
from calendar import monthrange
from collections import defaultdict
from dataclasses import replace
from datetime import date, timedelta
from math import fsum
from pathlib import Path

from purchase_planning.demand import apply_analog_levels, category_profiles, estimate_sku_demand
from purchase_planning.export import ExportValidationError, finite_quantity, read_export
from purchase_planning.facts import PlanFacts, Recommendation, SupplierSummary
from purchase_planning.inventory import forecast_period, project_stock
from purchase_planning.supply import classify_transit, read_supply

STOCK_HISTORY_START = date(2024, 1, 1)


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


def plan(data_dir: str | Path, as_of: date | None = None) -> PlanFacts:
    directory = Path(data_dir)
    tables = read_export(directory)
    reasons: list[str] = []
    sales_rows: list[tuple[str, date, float]] = []
    partial_months: set[date] = set()
    for row in tables["sales_monthly"]:
        try:
            month_end = _month_end(row["month"])
            quantity = finite_quantity(row["qty_sold"])
            if row["partial_month"] not in {"0", "1"}:
                raise ValueError
        except ValueError:
            reasons.append(
                f"sales_monthly.csv, SKU {row['sku_id']}: проверьте month (YYYY-MM), "
                "qty_sold (число) и partial_month (0 или 1)."
            )
            continue
        sales_rows.append((row["sku_id"], month_end, quantity))
        if row["partial_month"] == "1":
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
        [month for _, month, _ in sales_rows if month not in partial_months],
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
    skus_with_sales = set()
    for sku, month, quantity in sales_rows:
        if month <= calculation_date and month not in partial_months:
            sales[sku, month] += quantity
        if month == stock_month and know_month_sales:
            month_sales[sku] += quantity
        # A sale through the export cutoff is known even in an incomplete month.
        if quantity > 0 and (month <= calculation_date or (calculation_date == cutoff and month == stock_month)):
            skus_with_sales.add(sku)

    sku_rows = tables["sku_dim"]
    sku_categories = {row["sku_id"]: row["category"] for row in sku_rows}
    profiles = category_profiles(sku_categories, sales)
    history_months = _complete_stock_months(calculation_date, partial_months)
    own_demand = {
        sku: estimate_sku_demand(sku, profiles[category], history_months, sales, stock)
        for sku, category in sku_categories.items()
    }
    demand_by_sku = apply_analog_levels(sku_rows, own_demand, len(history_months), skus_with_sales)

    timing, receipts, open_lines = read_supply(tables, calculation_date, cutoff)
    transit, order_flags = classify_transit(open_lines, calculation_date)
    month_receipts: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    for receipt in receipts:
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
            current_stock, _ = project_stock(
                opening_stock, month_receipts[sku], profile, annual_level,
                start=month_start, end=forecast_start, snapshot_at=forecast_start,
            )

        arrivals = transit.get(sku, {})
        monthly_transit: dict[date, float] = defaultdict(float)
        for day, quantity in sorted(arrivals.items()):
            monthly_transit[day.replace(day=1)] += quantity
        supplier_timing = timing[supplier]
        lead, cycle = supplier_timing.lead_days, supplier_timing.cycle_days
        forecast = {}
        arrival: date | None = None
        arrival_stock: float | None = None
        order_qty = 0.0
        if lead is not None:
            arrival = calculation_date + timedelta(days=lead)
            horizon_end = forecast_start + timedelta(days=lead + (cycle or 0))
            forecast = dict(forecast_period(profile, annual_level, forecast_start, horizon_end))
            # Both the cutoff and the expected arrival are measured at the end of the day.
            arrival_stock, shortage = project_stock(
                current_stock, arrivals, profile, annual_level,
                start=forecast_start, end=horizon_end, snapshot_at=arrival + timedelta(days=1),
            )
            if cycle is not None:
                order_qty = shortage
        recommendation = Recommendation(
            sku_id=sku, supplier_id=supplier, order_qty=order_qty,
            excluded_stockout_months=demand.excluded_stockout_months, low_data=demand.low_data,
            stock_at_calculation=current_stock, arrival_date=arrival, stock_at_arrival=arrival_stock,
            in_transit_by_month=tuple(monthly_transit.items()),
            order_flags=tuple(sorted(order_flags.get(sku, set()))),
        )
        sku_plans.append((recommendation, forecast))
        quantities_by_supplier[supplier].append(order_qty)
        if order_qty > 0 and sku in skus_with_sales:
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
    return PlanFacts(
        cutoff, calculation_date, tuple(
            replace(row, monthly_forecast=tuple(forecast.get(month, 0.0) for month in forecast_months))
            for row, forecast in sku_plans
        ), warning, forecast_months, tuple(profiles.values()), suppliers, supply_warning,
    )
