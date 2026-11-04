from datetime import date
from pathlib import Path

from openpyxl import Workbook

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
    sheet.append((
        "Поставщик", "SKU", "К заказу", "Исключено дефицитных месяцев",
        *(f"Прогноз {month:%Y-%m}" for month in facts.forecast_months),
        "Мало данных",
    ))
    for row in facts.recommendations:
        sheet.append((
            row.supplier_id, row.sku_id, row.order_qty, row.excluded_stockout_months,
            *row.monthly_forecast, "Да" if row.low_data else None,
        ))
        for cell in sheet[sheet.max_row][:2]:
            cell.data_type = "s"

    workbook.save(path)
