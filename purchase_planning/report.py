from datetime import date
from itertools import groupby
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

from purchase_planning.facts import PlanFacts, Segment


def write_report(
    facts: PlanFacts, path: str | Path, *, run_date: date | None = None
) -> None:
    if run_date is None:
        run_date = date.today()

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Заказ по поставщикам"
    sheet.append(("Дата среза", facts.cutoff.isoformat()))
    status = "черновик, не размещать" if (run_date - facts.cutoff).days > 35 else None
    sheet.append(("Статус", status))
    if facts.cutoff_warning:
        sheet.append(("Предупреждение", facts.cutoff_warning))
    if facts.supply_warning:
        sheet.append(("Предупреждение", facts.supply_warning))
    headers = (
        "Поставщик", "SKU", "К заказу", "Исключено дефицитных месяцев",
        *(f"Прогноз {month:%Y-%m}" for month in facts.forecast_months),
        "Мало данных", "Плечо, дней", "Источник плеча", "Цикл, дней",
        "Остаток на дату расчёта", "Дата прихода нового заказа", "Остаток на дату прихода",
        "В пути по месяцам", "Флаги заказов", "Флаги поставщика", "Пора заказывать у поставщика",
        "Сегмент", "Новинка", "Наценка за 12 мес, %", "Продажи за 12 мес, шт.", "Флаги SKU",
    )
    sheet.append(headers)
    summaries = {supplier.supplier_id: supplier for supplier in facts.suppliers}
    segment_sheets = {Segment.PURCHASE: sheet}
    for segment in Segment:
        if segment != Segment.PURCHASE:
            segment_sheets[segment] = workbook.create_sheet(segment)
            segment_sheets[segment].append(headers)
    review = sorted(
        (row for row in facts.recommendations if row.segment == Segment.REVIEW),
        key=lambda row: (-row.sales_qty_12m, row.sku_id),
    )
    rows = sorted(
        (row for row in facts.recommendations if row.segment != Segment.REVIEW),
        key=lambda row: row.supplier_id,
    )
    rows.extend(review)
    for supplier, group in groupby(rows, key=lambda row: row.supplier_id):
        summary = summaries.get(supplier)
        timing = summary.timing if summary else None
        supplier_name = supplier or "Поставщик не указан"
        has_order_rows = False
        for row in group:
            target = segment_sheets[row.segment]
            has_order_rows |= row.segment == Segment.PURCHASE
            transit = ", ".join(f"{month:%Y-%m} — {quantity:g}" for month, quantity in row.in_transit_by_month)
            target.append((
                supplier_name, row.sku_id, row.order_qty, row.excluded_stockout_months,
                *row.monthly_forecast, "Да" if row.low_data else None,
                timing.lead_days if timing else None, timing.lead_source if timing else None,
                timing.cycle_days if timing else None, row.stock_at_calculation,
                row.arrival_date.isoformat() if row.arrival_date else None, row.stock_at_arrival,
                transit or None, ", ".join(row.order_flags) or None, None, None,
                row.segment, "Да" if row.is_new else None, row.markup_pct_12m, row.sales_qty_12m,
                ", ".join(row.sku_flags) or None,
            ))
            for cell in target[target.max_row][:2]:
                cell.data_type = "s"
        if summary is not None and has_order_rows:
            sheet.append((
                supplier_name, "Итого", summary.order_qty,
                *([None] * (headers.index("Флаги поставщика") - 3)),
                ", ".join(summary.timing.flags) or None, "Да" if summary.time_to_order else "Нет",
            ))
            sheet.cell(sheet.max_row, 1).data_type = "s"
            for cell in sheet[sheet.max_row]:
                cell.font = Font(bold=True)

    workbook.save(path)
