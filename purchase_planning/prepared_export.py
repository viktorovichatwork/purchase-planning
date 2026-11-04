"""Validated ERP export shared by current planning and historical backtests.

Preparation retains the complete history. Each calculation still selects only
information available on its own date; consumers must not mutate this snapshot.
"""

import json
from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from math import fsum
from pathlib import Path

from purchase_planning.export import ExportValidationError, finite_quantity, read_export
from purchase_planning.facts import ESTIMATED_CUTOFF_SOURCE
from purchase_planning.segments import Sale, month_index
from purchase_planning.supply import PurchaseLine, Receipt, read_supply_history


@dataclass(frozen=True)
class PreparedExport:
    directory: Path
    tables: dict[str, list[dict[str, str]]]
    sales_rows: list[tuple[str, Sale]]
    stock: dict[tuple[str, date], float]
    partial_months: set[date]
    orders: list[PurchaseLine]
    receipts: list[Receipt]
    cutoff: date
    cutoff_source: str


def prepare_export(data_dir: str | Path) -> PreparedExport:
    directory = Path(data_dir)
    tables = read_export(directory)
    sales, stock, partial = _read_monthly_history(tables)
    orders, receipts = read_supply_history(tables)
    cutoff, source = _read_cutoff(directory / "export_meta.json", sales, partial, orders, receipts)
    return PreparedExport(directory, tables, sales, stock, partial, orders, receipts, cutoff, source)


def _month_end(month: str) -> date:
    first = date.fromisoformat(f"{month}-01")
    return first.replace(day=monthrange(first.year, first.month)[1])


def _estimate_cutoff(
    monthly_totals: dict[int, float], complete_months: set[int], partial_month: date,
) -> date | None:
    month = month_index(partial_month)
    recent_months = sorted(complete_months)[-2:]
    prior_months = {month - 12, *(recent - 12 for recent in recent_months)}
    if len(recent_months) != 2 or not prior_months.issubset(complete_months):
        return None

    prior_recent = fsum(monthly_totals[recent - 12] for recent in recent_months)
    if prior_recent <= 0:
        return None
    growth = fsum(monthly_totals[recent] for recent in recent_months) / prior_recent
    expected = monthly_totals[month - 12] * growth
    if expected <= 0:
        return None
    days = partial_month.day
    day = round(days * monthly_totals[month] / expected)
    return partial_month.replace(day=max(1, min(day, days - 1)))


def _read_cutoff(
    path: Path, sales_rows: list[tuple[str, Sale]], partial_months: set[date],
    orders: list[PurchaseLine], receipts: list[Receipt],
) -> tuple[date, str]:
    try:
        contents = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        complete_months = {sale.month for _, sale in sales_rows} - partial_months
        latest_receipt = max((receipt.received for receipt in receipts), default=None)
        latest_order = max((line.placed for line in orders), default=None)
        last_month = max(complete_months | partial_months, default=None)
        if last_month is not None and last_month in partial_months:
            monthly_totals: dict[int, float] = defaultdict(float)
            for _, sale in sales_rows:
                monthly_totals[month_index(sale.month)] += sale.quantity
            estimate = _estimate_cutoff(monthly_totals, {month_index(month) for month in complete_months}, last_month)
            if estimate is not None:
                cutoff = max(day for day in (estimate, latest_receipt, latest_order) if day is not None)
                return cutoff, ESTIMATED_CUTOFF_SOURCE

        candidates = (
            (max(complete_months, default=None), "по последнему полному месяцу продаж"),
            (latest_receipt, "по последнему приходу"),
            (latest_order, "по последнему заказу"),
        )
        latest = max(((day, source) for day, source in candidates if day is not None), key=lambda item: item[0], default=None)
        if latest is None:
            raise ExportValidationError(["В выгрузке нет дат для определения даты среза."]) from None
        day, source = latest
        return day, f"{source} {day:%d.%m.%Y}"
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
    return cutoff, "по явно заданной дате выгрузки"


def _read_monthly_history(
    tables: dict[str, list[dict[str, str]]],
) -> tuple[list[tuple[str, Sale]], dict[tuple[str, date], float], set[date]]:
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
    return sales_rows, stock, partial_months
