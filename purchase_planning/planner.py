import json
from calendar import monthrange
from collections import defaultdict
from datetime import date
from math import isfinite
from pathlib import Path
from statistics import mean

from purchase_planning.export import ExportValidationError, read_export
from purchase_planning.facts import PlanFacts, Recommendation

# Временно, заменяется в тикете D (#6).
HORIZON_MONTHS = 6


def _month_end(month: str) -> date:
    first = date.fromisoformat(f"{month}-01")
    return first.replace(day=monthrange(first.year, first.month)[1])


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
    complete_sales: list[tuple[str, date, float]] = []
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
        if row["partial_month"] == "0":
            complete_sales.append((row["sku_id"], month_end, quantity))

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

    cutoff, warning = _read_cutoff(directory / "export_meta.json", [month for _, month, _ in complete_sales])
    calculation_date = cutoff if as_of is None else as_of
    if calculation_date > cutoff:
        raise ExportValidationError(["Дата расчёта позже даты среза."])

    sales: dict[str, list[float]] = defaultdict(list)
    for sku, month, quantity in complete_sales:
        if month <= calculation_date:
            sales[sku].append(quantity)

    stock_month = _month_end(calculation_date.strftime("%Y-%m"))
    recommendations = []
    for sku_row in sorted(tables["sku_dim"], key=lambda row: (row["supplier_id"], row["sku_id"])):
        sku = sku_row["sku_id"]
        monthly_sales = mean(sales[sku]) if sales[sku] else 0.0
        order_qty = max(0.0, monthly_sales * HORIZON_MONTHS - stock.get((sku, stock_month), 0.0))
        recommendations.append(Recommendation(sku, sku_row["supplier_id"], order_qty))
    return PlanFacts(cutoff, calculation_date, tuple(recommendations), warning)
