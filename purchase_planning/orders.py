"""Order quantities, batches and supplier priorities shared by all output formats."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from math import ceil, isclose

from purchase_planning.facts import PlanFacts, Recommendation


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


def stock_below_reorder_point(row: Recommendation) -> bool:
    return row.stock_at_calculation <= 0 or (
        row.stock_until is not None and row.arrival_date is not None and row.stock_until < row.arrival_date
    )


@dataclass(frozen=True)
class OrderBatch:
    deadline: date
    quantity: int
    seasonal: bool
    low_stock: bool = False


def _order_batches(row: Recommendation, next_order_date: date | None, as_of: date) -> list[OrderBatch]:
    """The seasonal and the ordinary part of a SKU order, each with its order-by date."""
    if row.order_qty <= 0:
        return []
    batches = []
    season = row.seasonal_order
    seasonal_qty = 0
    if season and season.actionable:
        seasonal_qty = rounded_quantity(season.order_qty, row.order_multiple)
        batches.append(OrderBatch(season.deadline, seasonal_qty, True, stock_below_reorder_point(row)))
    ordinary_qty = displayed_order_qty(row) - seasonal_qty
    if ordinary_qty > 0:
        placement = row.order_by if row.grouped_season_deadline else row.regular_order_date
        if row.regular_order_qty is None:
            placement = placement or row.order_by or next_order_date
        batches.append(OrderBatch(placement or as_of, ordinary_qty, False, stock_below_reorder_point(row)))
    return batches


@dataclass(frozen=True)
class SupplierSchedule:
    supplier_id: str
    lines: tuple[Recommendation, ...]
    batches: tuple[OrderBatch, ...]
    flags: tuple[str, ...]
    as_of: date

    @property
    def deadline(self) -> date:
        return min(batch.deadline for batch in self.batches)

    @property
    def quantity(self) -> int:
        return sum(batch.quantity for batch in self.batches)

    @property
    def due(self) -> bool:
        return bool(self.supplier_id) and self.deadline <= self.as_of

    @property
    def low_stock(self) -> bool:
        return any(batch.deadline <= self.as_of and batch.low_stock for batch in self.batches)

    @property
    def placements(self) -> tuple[OrderBatch, ...]:
        """Combine equal placement dates; overdue batches can only be placed now."""
        quantities: dict[tuple[bool, date], int] = defaultdict(int)
        for batch in self.batches:
            quantities[batch.seasonal, max(batch.deadline, self.as_of)] += batch.quantity
        return tuple(OrderBatch(day, qty, seasonal) for (seasonal, day), qty in sorted(quantities.items()))


@dataclass(frozen=True)
class OrderPlan:
    batches_by_sku: dict[str, tuple[OrderBatch, ...]]
    suppliers: tuple[SupplierSchedule, ...]


def prepare_orders(facts: PlanFacts) -> OrderPlan:
    summaries = {supplier.supplier_id: supplier for supplier in facts.suppliers}
    batches_by_sku: dict[str, tuple[OrderBatch, ...]] = {}
    groups: dict[str, list[Recommendation]] = defaultdict(list)
    for row in facts.recommendations:
        summary = summaries.get(row.supplier_id)
        batches = tuple(_order_batches(row, summary.next_order_date if summary else None, facts.as_of))
        batches_by_sku[row.sku_id] = batches
        if batches:
            groups[row.supplier_id].append(row)
    suppliers = []
    for supplier, rows in groups.items():
        lines = tuple(sorted(rows, key=lambda row: (-displayed_order_qty(row), row.sku_id)))
        batches = tuple(batch for row in lines for batch in batches_by_sku[row.sku_id])
        summary = summaries.get(supplier)
        suppliers.append(SupplierSchedule(
            supplier, lines, batches, summary.timing.flags if summary else (), facts.as_of,
        ))
    return OrderPlan(batches_by_sku, tuple(sorted(suppliers, key=lambda item: (item.deadline, item.supplier_id))))
