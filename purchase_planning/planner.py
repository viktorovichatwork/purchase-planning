import json
from calendar import monthrange
from collections import defaultdict
from datetime import date
from math import isfinite
from pathlib import Path

from purchase_planning.demand import apply_analog_levels, category_profiles, estimate_sku_demand
from purchase_planning.export import ExportValidationError, read_export
from purchase_planning.facts import PlanFacts, Recommendation

# Временно, заменяется в тикете D (#6).
HORIZON_MONTHS = 6
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


def _forecast_months(as_of: date) -> tuple[date, ...]:
    months = []
    for offset in range(HORIZON_MONTHS):
        year, month = divmod(as_of.year * 12 + as_of.month + offset, 12)
        months.append(date(year, month + 1, 1))
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


def _quantity(value: str) -> float:
    quantity = float(value)
    if not isfinite(quantity):
        raise ValueError
    return quantity


def plan(data_dir: str | Path, as_of: date | None = None) -> PlanFacts:
    directory = Path(data_dir)
    tables = read_export(directory)
    reasons: list[str] = []
    sales_rows: list[tuple[str, date, float]] = []
    partial_months: set[date] = set()
    for row in tables["sales_monthly"]:
        try:
            month_end = _month_end(row["month"])
            quantity = _quantity(row["qty_sold"])
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
            stock[row["sku_id"], _month_end(row["month"])] = _quantity(row["stock_qty"])
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
    sales: dict[tuple[str, date], float] = defaultdict(float)
    skus_with_sales = set()
    for sku, month, quantity in sales_rows:
        if month <= calculation_date and month not in partial_months:
            sales[sku, month] += quantity
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
    forecast_months = _forecast_months(calculation_date)
    recommendations = []
    for sku_row in sorted(sku_rows, key=lambda row: (row["supplier_id"], row["sku_id"])):
        sku = sku_row["sku_id"]
        profile = profiles[sku_row["category"]]
        demand = demand_by_sku[sku]
        forecast = tuple(
            (demand.annual_level or 0.0) * profile.weight_for(month.month)
            for month in forecast_months
        )
        order_qty = max(0.0, sum(forecast) - stock.get((sku, stock_month), 0.0))
        recommendations.append(Recommendation(
            sku, sku_row["supplier_id"], order_qty, demand.excluded_stockout_months,
            forecast, demand.low_data,
        ))
    return PlanFacts(
        cutoff, calculation_date, tuple(recommendations), warning, forecast_months,
        tuple(profiles.values()),
    )
