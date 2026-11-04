"""One set of SKU counts for the portal tiles, the tab intros and the XLSX «Сводка» sheet.

SKU counts match the relevant group in the linked tab; unit and supplier totals accompany the order intro.
"""

from dataclasses import dataclass
from datetime import date, timedelta

from purchase_planning.actions import Action, SkipReason, action, skip_reason
from purchase_planning.facts import PlanFacts, Recommendation
from purchase_planning.order_sheet import displayed_order_qty, excess_rows, rounded_quantity, stockout_rows
from purchase_planning.wording import days_text

ORDER_SOON_DAYS = 30
SEASON_SOON_DAYS = 90


@dataclass(frozen=True)
class OrderBatch:
    deadline: date
    quantity: int
    seasonal: bool


def order_batches(row: Recommendation, next_order_date: date | None, as_of: date) -> list[OrderBatch]:
    """The seasonal and the ordinary part of a SKU order, each with its order-by date."""
    if row.order_qty <= 0:
        return []
    batches = []
    season = row.seasonal_order
    seasonal_qty = 0
    if season and season.actionable:
        seasonal_qty = rounded_quantity(season.order_qty, row.order_multiple)
        batches.append(OrderBatch(season.deadline, seasonal_qty, True))
    ordinary_qty = displayed_order_qty(row) - seasonal_qty
    if ordinary_qty > 0:
        placement = row.order_by if row.grouped_season_deadline else row.regular_order_date
        if row.regular_order_qty is None:
            placement = placement or row.order_by or next_order_date
        batches.append(OrderBatch(placement or as_of, ordinary_qty, False))
    return batches


def season_deadline(row: Recommendation, as_of: date) -> date | None:
    """The seasonal order-by date the buyer still can meet: this season's, or next year's once it has passed."""
    season = row.seasonal_order
    if season is None:
        return None
    return season.deadline if season.deadline >= as_of else season.next_deadline


def season_order_soon(row: Recommendation, as_of: date) -> bool:
    """A positive seasonal order whose actual deadline is within the next 90 days."""
    season = row.seasonal_order
    return bool(
        season and season.actionable
        and as_of <= season.deadline <= as_of + timedelta(days=SEASON_SOON_DAYS)
    )


@dataclass(frozen=True)
class PlanSummary:
    order_now: int
    order_now_units: int
    order_now_suppliers: int
    due_suppliers: int
    order_soon: int
    order_later: int
    order_not_needed: int
    decide: int
    skip: dict[SkipReason, int]
    risks: int
    excess: int
    season_soon: int

    @property
    def order_total(self) -> int:
        return self.order_now + self.order_soon + self.order_later + self.order_not_needed

    @property
    def skip_total(self) -> int:
        return sum(self.skip.values())


@dataclass(frozen=True)
class Tile:
    label: str
    count: int
    view: str
    main: bool


def plan_summary(facts: PlanFacts) -> PlanSummary:
    as_of = facts.as_of
    soon_until = as_of + timedelta(days=ORDER_SOON_DAYS)
    next_dates = {supplier.supplier_id: supplier.next_order_date for supplier in facts.suppliers}
    now = soon = later = not_needed = units = 0
    suppliers: set[str] = set()
    due_suppliers: set[str] = set()
    for row in facts.recommendations:
        batches = order_batches(row, next_dates.get(row.supplier_id), as_of)
        due = [batch for batch in batches if batch.deadline <= as_of]
        # A supplier is due when any of its SKU, ordered or to decide, is due. Without a name nobody can order.
        if due and row.supplier_id:
            due_suppliers.add(row.supplier_id)
        if action(row) != Action.ORDER:
            continue
        if due:
            now += 1
            units += sum(batch.quantity for batch in due)
            if row.supplier_id:
                suppliers.add(row.supplier_id)
        elif any(batch.deadline <= soon_until for batch in batches):
            soon += 1
        elif batches:
            later += 1
        else:
            not_needed += 1
    skip = dict.fromkeys(SkipReason, 0)
    for row in facts.recommendations:
        if (reason := skip_reason(row)) is not None:
            skip[reason] += 1
    return PlanSummary(
        order_now=now, order_now_units=units, order_now_suppliers=len(suppliers), due_suppliers=len(due_suppliers),
        order_soon=soon, order_later=later, order_not_needed=not_needed,
        decide=sum(action(row) == Action.DECIDE for row in facts.recommendations),
        skip=skip, risks=len(stockout_rows(facts)), excess=len(excess_rows(facts)),
        season_soon=sum(season_order_soon(row, as_of) for row in facts.recommendations),
    )


def summary_tiles(summary: PlanSummary) -> tuple[Tile, ...]:
    return (
        Tile("Пора заказывать, SKU", summary.order_now, "orders", True),
        Tile(f"{Action.DECIDE}, SKU", summary.decide, "decide", True),
        Tile(f"{Action.SKIP}, SKU", summary.skip_total, "skip", True),
        Tile("Риск дефицита, SKU", summary.risks, "risks", True),
        Tile(f"Заказать в ближайшие {days_text(ORDER_SOON_DAYS)}, SKU", summary.order_soon, "orders", False),
        Tile(f"Сезонный заказ в ближайшие {days_text(SEASON_SOON_DAYS)}, SKU", summary.season_soon, "seasons", False),
        Tile("Избыток, SKU", summary.excess, "excess", False),
    )
