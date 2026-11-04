from itertools import groupby
from math import ceil

from purchase_planning.facts import PlanFacts, Recommendation


def displayed_order_qty(row: Recommendation) -> int:
    return ceil(row.order_qty)


def supplier_orders(facts: PlanFacts) -> list[tuple[str, list[Recommendation], int]]:
    due = {supplier.supplier_id for supplier in facts.suppliers if supplier.time_to_order}
    rows = sorted(
        (row for row in facts.recommendations if row.order_qty > 0),
        key=lambda row: (row.supplier_id not in due, row.supplier_id, -row.order_qty, row.sku_id),
    )
    orders = []
    for supplier, group in groupby(rows, key=lambda row: row.supplier_id):
        lines = list(group)
        orders.append((supplier, lines, sum(displayed_order_qty(row) for row in lines)))
    return orders
