from datetime import date
from itertools import groupby
from math import ceil, isclose

from purchase_planning.facts import PlanFacts, Recommendation
from purchase_planning.wording import days_text

SupplierOrder = tuple[str, list[Recommendation], int]


def rounded_quantity(quantity: float, multiple: int) -> int:
    packs = quantity / multiple
    nearest = round(packs)
    if nearest > 0 and isclose(packs, nearest, rel_tol=1e-12):
        return nearest * multiple
    return ceil(packs) * multiple


def displayed_order_qty(row: Recommendation) -> int:
    if row.seasonal_order and row.seasonal_order.actionable and row.regular_order_qty:
        return rounded_quantity(row.regular_order_qty, row.order_multiple) + rounded_quantity(
            row.seasonal_order.order_qty, row.order_multiple,
        )
    return rounded_quantity(row.order_qty, row.order_multiple)


def order_date_label(
    day: date, as_of: date, *, date_format: str = "%Y-%m-%d", below_reorder_point: bool = False,
) -> str:
    if day > as_of:
        return day.strftime(date_format)
    return "Сейчас · запас ниже точки заказа" if below_reorder_point else "Сейчас"


def stock_below_reorder_point(row: Recommendation) -> bool:
    return row.stock_at_calculation <= 0 or (
        row.stock_until is not None and row.arrival_date is not None and row.stock_until < row.arrival_date
    )


def open_orders_text(row: Recommendation, as_of: date) -> str:
    """Open supplier orders in plain words: when each one arrives or how late it is."""
    parts = []
    for promised, quantity in row.open_orders:
        amount = f"{quantity:.0f} шт."
        if promised is None:
            parts.append(f"{amount} — в ERP нет верной даты прихода, в расчёт не взято")
        elif promised == as_of:
            parts.append(f"{amount} — придёт {promised:%d.%m.%Y}, в день выгрузки")
        elif promised > as_of:
            parts.append(f"{amount} — придёт {promised:%d.%m.%Y}, через {days_text((promised - as_of).days)}")
        else:
            parts.append(f"{amount} — опаздывает на {days_text((as_of - promised).days)}, в расчёт не взято")
    return "; ".join(parts) or "—"


def coverage_text(row: Recommendation) -> str:
    if row.coverage_months is None:
        return "Нет прогноза"
    prefix = "> " if row.stock_until is None else ""
    return prefix + f"{row.coverage_months:.1f}".replace(".", ",")


def supplier_orders(facts: PlanFacts) -> list[SupplierOrder]:
    due = {supplier.supplier_id for supplier in facts.suppliers if supplier.time_to_order}
    rows = sorted(
        (row for row in facts.recommendations if row.order_qty > 0),
        key=lambda row: (row.supplier_id not in due, row.supplier_id, -displayed_order_qty(row), row.sku_id),
    )
    orders = []
    for supplier, group in groupby(rows, key=lambda row: row.supplier_id):
        lines = list(group)
        orders.append((supplier, lines, sum(displayed_order_qty(row) for row in lines)))
    return orders


def stockout_rows(facts: PlanFacts) -> list[Recommendation]:
    return sorted(
        (row for row in facts.recommendations if row.stockout_risk),
        key=lambda row: (row.stock_until or date.max, -row.lost_sales, row.sku_id),
    )


def excess_rows(facts: PlanFacts) -> list[Recommendation]:
    return sorted(
        (row for row in facts.recommendations if row.excess),
        key=lambda row: (-(row.coverage_months or 0) * (row.price_decile or 0), row.sku_id),
    )
