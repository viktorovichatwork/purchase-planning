from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum
from math import ceil
from typing import NotRequired, TypedDict

from purchase_planning.actions import segment_action
from purchase_planning.segments import Sale
from purchase_planning.supply import PurchaseLine, Receipt


class Driver(StrEnum):
    SALES = "sales"
    RECEIPT = "receipt"
    ORDER_CANCELLED = "order_cancelled"
    PARAMETER_CHANGED = "parameter_changed"


DRIVER_LABELS = {
    Driver.SALES: "Продажи",
    Driver.RECEIPT: "Приход",
    Driver.ORDER_CANCELLED: "Отмена остатка заказа",
    Driver.PARAMETER_CHANGED: "Смена параметра",
}


class Evidence(TypedDict):
    sales: list[list[str | float | bool | None]]
    receipts: list[list[str | float]]
    open_orders: list[list[str]]
    closed_unfilled_orders: list[list[str]]


class ComparedRow(TypedDict):
    sku_id: str
    order_qty: float
    rounded_order_qty: NotRequired[int]
    segment: str
    stockout_risk: bool


class Change(TypedDict):
    sku_id: str
    order_qty: tuple[float, float]
    segment: tuple[str | None, str | None]
    stockout_risk: tuple[bool, bool]
    drivers: list[Driver]
    flags: list[str]


@dataclass(frozen=True)
class ChangeCounts:
    increased: int
    decreased: int
    segments: int
    new_risks: int
    resolved_risks: int
    unconfirmed: int
    by_driver: Counter[Driver]

    @property
    def quantities(self) -> int:
        return self.increased + self.decreased


def count_changes(changes: list[Change]) -> ChangeCounts:
    return ChangeCounts(
        increased=sum(old < new for old, new in (row["order_qty"] for row in changes)),
        decreased=sum(old > new for old, new in (row["order_qty"] for row in changes)),
        # The buyer sees action groups, so a move between two «don't order» segments is not a change.
        segments=sum(segment_action(old) != segment_action(new) for old, new in (row["segment"] for row in changes)),
        new_risks=sum(not old and new for old, new in (row["stockout_risk"] for row in changes)),
        resolved_risks=sum(old and not new for old, new in (row["stockout_risk"] for row in changes)),
        unconfirmed=sum(bool(row["flags"]) for row in changes),
        by_driver=Counter(code for row in changes for code in set(row["drivers"])),
    )


def comparison_evidence(
    sales: dict[str, list[Sale]], receipts: list[Receipt], orders: list[PurchaseLine],
) -> dict[str, Evidence]:
    by_sku: dict[str, Evidence] = defaultdict(
        lambda: Evidence(sales=[], receipts=[], open_orders=[], closed_unfilled_orders=[]),
    )
    for sku, rows in sales.items():
        for sale in sorted(rows, key=lambda row: (
            row.month, row.quantity, row.markup_pct is None, row.markup_pct or 0, row.partial,
        )):
            by_sku[sku]["sales"].append([
                sale.month.isoformat(), sale.quantity, sale.markup_pct, sale.partial,
            ])
    for receipt in sorted(receipts, key=lambda row: (row.received, row.supplier, row.qty)):
        by_sku[receipt.sku]["receipts"].append([receipt.received.isoformat(), receipt.supplier, receipt.qty])

    counts = Counter((order.sku, order.supplier, order.po_id) for order in orders)
    for order in sorted(orders, key=lambda row: (row.supplier, row.po_id)):
        if not order.po_id or counts[order.sku, order.supplier, order.po_id] != 1:
            continue
        key = [order.supplier, order.po_id]
        if order.status == "open" and order.pending > 0:
            by_sku[order.sku]["open_orders"].append(key)
        elif order.status == "closed" and order.received < order.ordered:
            by_sku[order.sku]["closed_unfilled_orders"].append(key)
    return dict(by_sku)


def compare(
    previous: list[ComparedRow], current: list[ComparedRow],
    old_evidence: dict[str, Evidence], new_evidence: dict[str, Evidence], *,
    parameters_changed: bool,
) -> list[Change]:
    before = {row["sku_id"]: row for row in previous}
    after = {row["sku_id"]: row for row in current}
    empty = Evidence(sales=[], receipts=[], open_orders=[], closed_unfilled_orders=[])
    changes = []
    for sku in sorted(before.keys() | after.keys()):
        old, new = before.get(sku), after.get(sku)
        order_qty = (
            old.get("rounded_order_qty", ceil(old["order_qty"])) if old else 0.0,
            new.get("rounded_order_qty", ceil(new["order_qty"])) if new else 0.0,
        )
        segment = (old["segment"] if old else None, new["segment"] if new else None)
        risk = (old["stockout_risk"] if old else False, new["stockout_risk"] if new else False)
        if order_qty[0] == order_qty[1] and segment[0] == segment[1] and risk[0] == risk[1]:
            continue

        old_input = old_evidence.get(sku, empty)
        new_input = new_evidence.get(sku, empty)
        drivers = []
        if old_input["sales"] != new_input["sales"]:
            drivers.append(Driver.SALES)
        if old_input["receipts"] != new_input["receipts"]:
            drivers.append(Driver.RECEIPT)
        open_orders = {tuple(key) for key in old_input["open_orders"]}
        if any(tuple(key) in open_orders for key in new_input["closed_unfilled_orders"]):
            drivers.append(Driver.ORDER_CANCELLED)
        if parameters_changed:
            drivers.append(Driver.PARAMETER_CHANGED)
        changes.append(Change(
            sku_id=sku, order_qty=order_qty, segment=segment, stockout_risk=risk,
            drivers=drivers, flags=[] if drivers else ["Причина не подтверждена данными"],
        ))
    return changes


def summarize(previous: str | None, changes: list[Change]) -> str:
    if previous is None:
        return "Первый запуск, истории для сравнения нет."
    if not changes:
        return "Изменений нет."

    counts = count_changes(changes)
    causes = ", ".join(
        f"{label} — {counts.by_driver[code]}" for code, label in DRIVER_LABELS.items() if counts.by_driver[code]
    )
    return (
        f"К заказу изменилось у {counts.quantities} SKU, группа («Заказать», «Решить закупщику», «Не заказывать») — у {counts.segments}. "
        f"Новые риски — {counts.new_risks}, ушедшие — {counts.resolved_risks}.\n"
        f"Наблюдаемые причины — {causes or 'нет'}. Без подтверждённой причины — {counts.unconfirmed}."
    )
