from collections.abc import Mapping
from datetime import date
from math import fsum
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

from purchase_planning.facts import PlanFacts, Segment
from purchase_planning.order_sheet import displayed_order_qty, supplier_orders
from purchase_planning.parameters import STALE_EXPORT_DAYS, run_parameters


def write_report(
    facts: PlanFacts, path: str | Path, *, run_date: date | None = None,
    parameters: Mapping[str, str | int | float] | None = None,
) -> None:
    if run_date is None:
        run_date = date.today()

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Заказ по поставщикам"
    sheet.append(("Дата среза", facts.cutoff.isoformat()))
    status = "черновик, не размещать" if (run_date - facts.cutoff).days > STALE_EXPORT_DAYS else None
    sheet.append(("Статус", status))
    if facts.cutoff_warning:
        sheet.append(("Предупреждение", facts.cutoff_warning))
    if facts.supply_warning:
        sheet.append(("Предупреждение", facts.supply_warning))
    headers = (
        "Поставщик", "SKU", "К заказу", "Исключено дефицитных месяцев",
        "Прогноз на горизонт, шт.",
        "Мало данных", "Плечо, дней", "Источник плеча", "Цикл, дней",
        "Остаток на дату расчёта", "Дата прихода нового заказа", "Остаток на дату прихода",
        "В пути по месяцам", "Флаги заказов", "Флаги поставщика", "Пора заказывать у поставщика",
        "Сегмент", "Новинка", "Наценка за 12 мес, %", "Продажи за 12 мес, шт.",
        "Оценка спроса в год, шт.", "Флаги SKU",
    )
    sheet.append(headers)
    summaries = {supplier.supplier_id: supplier for supplier in facts.suppliers}
    forecast_sheet = workbook.create_sheet("Прогноз")
    forecast_sheet.append(("Поставщик", "SKU", *(f"Прогноз {month:%Y-%m}" for month in facts.forecast_months)))
    segment_sheets = {}
    for segment in Segment:
        if segment != Segment.PURCHASE:
            segment_sheets[segment] = workbook.create_sheet(segment)
            segment_sheets[segment].append(headers)
    def append_sku(target, row):
        summary = summaries.get(row.supplier_id)
        timing = summary.timing if summary else None
        transit = ", ".join(f"{month:%Y-%m} — {round(quantity)}" for month, quantity in row.in_transit_by_month)
        target.append((
            row.supplier_id or "Поставщик не указан", row.sku_id, displayed_order_qty(row), row.excluded_stockout_months,
            round(fsum(row.monthly_forecast)), "Да" if row.low_data else None,
            timing.lead_days if timing else None, timing.lead_source if timing else None,
            timing.cycle_days if timing else None, round(row.stock_at_calculation),
            row.arrival_date.isoformat() if row.arrival_date else None,
            round(row.stock_at_arrival) if row.stock_at_arrival is not None else None,
            transit or None, ", ".join(row.order_flags) or None,
            (", ".join(timing.flags) or None) if timing else None,
            ("Да" if summary.time_to_order else "Нет") if summary else None,
            row.segment, "Да" if row.is_new else None,
            round(row.markup_pct_12m, 1) if row.markup_pct_12m is not None else None,
            round(row.sales_qty_12m), round(row.annual_demand_qty), ", ".join(row.sku_flags) or None,
        ))
        for cell in target[target.max_row][:2]:
            cell.data_type = "s"
        target.cell(target.max_row, headers.index("Наценка за 12 мес, %") + 1).number_format = "0.0"

    for supplier, group, total in supplier_orders(facts):
        for row in group:
            append_sku(sheet, row)
        summary = summaries.get(supplier)
        sheet.append((
            supplier or "Поставщик не указан", "Итого", total,
            *([None] * (headers.index("Флаги поставщика") - 3)),
            (", ".join(summary.timing.flags) or None) if summary else None,
            ("Да" if summary.time_to_order else "Нет") if summary else None,
        ))
        sheet.cell(sheet.max_row, 1).data_type = "s"
        for cell in sheet[sheet.max_row]:
            cell.font = Font(bold=True)

    for row in sorted(facts.recommendations, key=lambda row: (row.supplier_id, row.sku_id)):
        forecast_sheet.append((
            row.supplier_id or "Поставщик не указан", row.sku_id, *(round(qty) for qty in row.monthly_forecast),
        ))
        for cell in forecast_sheet[forecast_sheet.max_row][:2]:
            cell.data_type = "s"
        if row.segment not in {Segment.PURCHASE, Segment.REVIEW}:
            append_sku(segment_sheets[row.segment], row)
    review = sorted(
        (row for row in facts.recommendations if row.segment == Segment.REVIEW),
        key=lambda row: (-row.sales_qty_12m, row.sku_id),
    )
    for row in review:
        append_sku(segment_sheets[Segment.REVIEW], row)

    parameter_sheet = workbook.create_sheet("Параметры")
    parameter_sheet.append(("Параметр", "Значение"))
    parameter_sheet.append(("Дата среза", facts.cutoff.isoformat()))
    parameter_sheet.append(("Дата расчёта", facts.as_of.isoformat()))
    parameter_sheet.append(("Дата запуска", run_date.isoformat()))
    for name, value in (run_parameters() if parameters is None else parameters).items():
        parameter_sheet.append((name, value))
    parameter_sheet.column_dimensions["A"].width = 60
    parameter_sheet.column_dimensions["B"].width = 115
    workbook.save(path)
