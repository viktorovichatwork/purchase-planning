from itertools import groupby
from math import ceil, isclose

from purchase_planning.facts import PlanFacts, Recommendation


def displayed_order_qty(row: Recommendation) -> int:
    packs = row.order_qty / row.order_multiple
    nearest = round(packs)
    if nearest > 0 and isclose(packs, nearest, rel_tol=1e-12):
        return nearest * row.order_multiple
    return ceil(packs) * row.order_multiple


def supplier_orders(facts: PlanFacts) -> list[tuple[str, list[Recommendation], int]]:
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
