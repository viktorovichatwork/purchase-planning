from datetime import date
from itertools import groupby
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

from purchase_planning.facts import PlanFacts


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
    )
    sheet.append(headers)
    summaries = {supplier.supplier_id: supplier for supplier in facts.suppliers}
    rows = sorted(facts.recommendations, key=lambda row: row.supplier_id)
    for supplier, group in groupby(rows, key=lambda row: row.supplier_id):
        summary = summaries.get(supplier)
        timing = summary.timing if summary else None
        supplier_name = supplier or "Поставщик не указан"
        for row in group:
            transit = ", ".join(f"{month:%Y-%m} — {quantity:g}" for month, quantity in row.in_transit_by_month)
            sheet.append((
                supplier_name, row.sku_id, row.order_qty, row.excluded_stockout_months,
                *row.monthly_forecast, "Да" if row.low_data else None,
                timing.lead_days if timing else None, timing.lead_source if timing else None,
                timing.cycle_days if timing else None, row.stock_at_calculation,
                row.arrival_date.isoformat() if row.arrival_date else None, row.stock_at_arrival,
                transit or None, ", ".join(row.order_flags) or None,
            ))
            for cell in sheet[sheet.max_row][:2]:
                cell.data_type = "s"
        if summary is not None:
            sheet.append((
                supplier_name, "Итого", summary.order_qty,
                *([None] * (len(headers) - 5)),
                ", ".join(summary.timing.flags) or None, "Да" if summary.time_to_order else "Нет",
            ))
            sheet.cell(sheet.max_row, 1).data_type = "s"
            for cell in sheet[sheet.max_row]:
                cell.font = Font(bold=True)

    workbook.save(path)
