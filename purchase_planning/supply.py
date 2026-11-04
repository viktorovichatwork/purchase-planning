from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from math import ceil, isclose
from statistics import median, quantiles

from purchase_planning.export import ExportValidationError, finite_quantity
from purchase_planning.facts import SupplierTiming

LEAD_HISTORY_START = date(2025, 1, 1)
MIN_ORDERS = 5
DEFAULT_DELAY_DAYS = 20
MIN_IMPORT_LEAD_DAYS = 30
MIN_CYCLE_DAYS = 30
MAX_CYCLE_DAYS = 120
MAX_TRANSIT_OVERDUE_DAYS = 30
ORDER_TAIL_SHARE = 0.10


@dataclass(frozen=True)
class PurchaseLine:
    po_id: str
    sku: str
    supplier: str
    placed: date
    promised: date | None
    planned_lead_days: float | None
    status: str
    ordered: float
    received: float
    pending: float

    @property
    def batch(self) -> str | tuple[date, date | None]:
        return self.po_id or (self.placed, self.promised)


@dataclass(frozen=True)
class Receipt:
    sku: str
    supplier: str
    received: date
    qty: float


def _purchase_line(row: dict[str, str]) -> PurchaseLine:
    try:
        placed = date.fromisoformat(row["order_date"])
        promised = date.fromisoformat(row["promised_date"]) if row["promised_date"] else None
        planned = row["planned_lead_days"]
        planned_lead_days = finite_quantity(planned) if planned else None
        ordered = finite_quantity(row["qty_ordered"])
        received = finite_quantity(row["qty_received"])
        pending = finite_quantity(row["qty_pending"])
    except ValueError:
        raise ExportValidationError([
            f"purchase_orders.csv, SKU {row['sku_id']}: проверьте даты, плановый срок и количества заказа."
        ]) from None
    return PurchaseLine(
        row["po_id"], row["sku_id"], row["supplier_id"], placed, promised, planned_lead_days,
        row["status"], ordered, received, pending,
    )


def _receipt(row: dict[str, str]) -> Receipt:
    try:
        return Receipt(
            row["sku_id"], row["supplier_id"], date.fromisoformat(row["receipt_date"]),
            finite_quantity(row["qty"]),
        )
    except ValueError:
        raise ExportValidationError([
            f"receipts.csv, SKU {row['sku_id']}: проверьте дату и количество прихода."
        ]) from None


def _matched_receipt_dates(
    orders: list[PurchaseLine], receipts: list[Receipt],
) -> dict[PurchaseLine, date]:
    # ERP omits po_id from receipts. Use a full-quantity match only when unique in both directions.
    orders_by_item: dict[tuple[str, float], list[PurchaseLine]] = defaultdict(list)
    for line in orders:
        if line.ordered > 0 and line.received >= line.ordered:
            orders_by_item[line.sku, line.ordered].append(line)
    dates: dict[PurchaseLine, list[date]] = defaultdict(list)
    ambiguous: set[PurchaseLine] = set()
    for receipt in receipts:
        candidates = [
            line for line in orders_by_item[receipt.sku, receipt.qty]
            if line.placed <= receipt.received
        ]
        if len(candidates) == 1:
            dates[candidates[0]].append(receipt.received)
        else:
            ambiguous.update(candidates)
    return {line: days[0] for line, days in dates.items() if len(days) == 1 and line not in ambiguous}


def _median_interval(dates: set[date]) -> float | None:
    days = sorted(dates)
    if len(days) < 2:
        return None
    return median((end - start).days for start, end in zip(days, days[1:]))


def _supplier_timing(
    orders: list[PurchaseLine], receipts: list[Receipt], supplier_row: dict[str, str], *, use_summary: bool,
) -> SupplierTiming:
    promises: dict[str | tuple[date, date | None], list[int]] = defaultdict(list)
    delays: dict[str | tuple[date, date | None], int] = {}
    recent = [line for line in orders if line.placed >= LEAD_HISTORY_START]
    enough_recent_batches = len({line.batch for line in recent}) >= MIN_ORDERS
    matched = _matched_receipt_dates(orders, receipts) if enough_recent_batches else {}
    for line in recent:
        if line.promised is None or line.promised < line.placed:
            continue
        promises[line.batch].append((line.promised - line.placed).days)
        if line in matched:
            delay = max(0, (matched[line] - line.promised).days)
            delays[line.batch] = max(delays.get(line.batch, 0), delay)

    flags = []
    lead_days: int | None
    if enough_recent_batches and delays:
        promised_days = median(median(days) for days in promises.values())
        observations = list(delays.values())
        p75 = quantiles(observations, n=4, method="inclusive")[2] if len(observations) > 1 else observations[0]
        lead_days, source = ceil(promised_days + p75), "История заказов + p75"
        if len(delays) < len(promises):
            flags.append("Опоздание оценено по доступным приходам")
    else:
        planned_days = [line.planned_lead_days for line in orders if line.planned_lead_days is not None]
        fallback_days: float | None
        if enough_recent_batches and promises:
            fallback_days = median(median(days) for days in promises.values())
            source = "Заказы + 20 дней"
        elif planned_days:
            fallback_days = median(planned_days)
            source = "Вся история заказов + 20 дней"
        else:
            summary_value = supplier_row.get("median_planned_lead_days", "") if use_summary else ""
            try:
                fallback_days = finite_quantity(summary_value) if summary_value else None
            except ValueError:
                raise ExportValidationError([
                    "supplier_dim.csv: проверьте median_planned_lead_days для "
                    f"{supplier_row['supplier_id']}."
                ]) from None
            if fallback_days is not None and fallback_days < 0:
                fallback_days = None
            source = "Справочник + 20 дней"

        if fallback_days is None:
            lead_days, source = None, "Нет данных"
            flags.append("Нет обещанного срока")
        elif fallback_days + DEFAULT_DELAY_DAYS < 0:
            lead_days = None
            flags.append("Отрицательная оценка плеча")
        else:
            lead_days = ceil(fallback_days + DEFAULT_DELAY_DAYS)
        if enough_recent_batches:
            flags.append("Нет сопоставимых приходов для p75")

    if lead_days is not None and lead_days < MIN_IMPORT_LEAD_DAYS and supplier_row.get("supply_type") == "import":
        source += " · проверить плечо"

    cycle = _median_interval({line.placed for line in orders})
    if cycle is None:
        cycle = _median_interval({receipt.received for receipt in receipts})
    if cycle is None:
        flags.append("Нет истории для цикла")
    elif not MIN_CYCLE_DAYS <= cycle <= MAX_CYCLE_DAYS:
        flags.append(f"цикл ограничен (было {cycle:g} дней)")
        cycle = min(MAX_CYCLE_DAYS, max(MIN_CYCLE_DAYS, cycle))
    return SupplierTiming(lead_days, source, ceil(cycle) if cycle is not None else None, tuple(flags))


def read_supply(
    tables: dict[str, list[dict[str, str]]], as_of: date, cutoff: date,
) -> tuple[dict[str, SupplierTiming], list[Receipt], list[PurchaseLine]]:
    orders_by_supplier: dict[str, list[PurchaseLine]] = defaultdict(list)
    for row in tables["purchase_orders"]:
        line = _purchase_line(row)
        if line.placed <= as_of:
            orders_by_supplier[line.supplier].append(line)
    receipts_by_supplier: dict[str, list[Receipt]] = defaultdict(list)
    for row in tables["receipts"]:
        receipt = _receipt(row)
        if receipt.received <= as_of and receipt.qty > 0:
            receipts_by_supplier[receipt.supplier].append(receipt)

    # Supplier summary fields and PO status/quantities are snapshots at the export cutoff.
    supplier_rows = {row["supplier_id"]: row for row in tables["supplier_dim"]}
    timing = {
        supplier: _supplier_timing(
            orders_by_supplier[supplier], receipts_by_supplier[supplier], supplier_rows.get(supplier, {}),
            use_summary=as_of == cutoff,
        )
        for supplier in sorted({row["supplier_id"] for row in tables["sku_dim"]})
    }
    receipts = [receipt for group in receipts_by_supplier.values() for receipt in group]
    orders = [
        line for group in orders_by_supplier.values() for line in group if as_of == cutoff
    ]
    return timing, receipts, orders


def classify_transit(
    open_lines: list[PurchaseLine], as_of: date,
) -> tuple[dict[str, dict[date, float]], dict[str, set[str]]]:
    arrivals: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    flags: dict[str, set[str]] = defaultdict(set)
    for line in open_lines:
        if line.promised is None or line.promised < line.placed:
            flags[line.sku].add("Нет корректной даты прихода")
        elif (as_of - line.promised).days <= MAX_TRANSIT_OVERDUE_DAYS:
            if line.pending > 0:
                expected = max(line.promised, as_of + timedelta(days=1))
                arrivals[line.sku][expected] += line.pending
                if line.promised < as_of:
                    flags[line.sku].add("Дата прихода просроченного заказа оценена")
        elif line.ordered <= 0:
            flags[line.sku].add("Нет количества заказа для проверки недопоставки")
        elif line.received < line.ordered:
            shortage = line.ordered - line.received
            limit = line.ordered * ORDER_TAIL_SHARE
            is_tail = shortage <= limit or isclose(shortage, limit, rel_tol=1e-12)
            flags[line.sku].add("Хвост заказа" if is_tail else "Зависший заказ")
    return arrivals, flags
