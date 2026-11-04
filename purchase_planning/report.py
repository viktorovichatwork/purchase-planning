from collections.abc import Mapping
from datetime import date
from math import fsum
from pathlib import Path

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Font
from openpyxl.worksheet.worksheet import Worksheet

from purchase_planning.actions import Action, SkipReason, action, skip_reason, todo_text, why_text
from purchase_planning.calibration import METRIC_HEADERS, stock_multiplier
from purchase_planning.facts import (
    TYPICAL_TIMING_FLAG, UNCONFIRMED_ORDERS_LABEL, OrderFlag, PlanFacts, Recommendation, Segment,
)
from purchase_planning.order_sheet import (
    coverage_text, displayed_order_qty, excess_rows, open_orders_text, order_date_label, rounded_quantity,
    stock_below_reorder_point,
    stockout_rows, supplier_orders,
)
from purchase_planning.parameters import (
    PARAMETER_NUMBER_FORMATS, STALE_EXPORT_DAYS, STALE_EXPORT_STATUS, llm_max_skus, run_parameters,
)
from purchase_planning.replenishment import inventory_summary
from purchase_planning.sku_labels import category_name
from purchase_planning.summary import ORDER_SOON_DAYS, order_batches, plan_summary, season_order_soon, summary_tiles
from purchase_planning.wording import days_text


def _stock_until_label(row: Recommendation) -> str | None:
    if row.stock_until:
        return row.stock_until.isoformat()
    if row.coverage_months is not None:
        return "Дольше горизонта"
    return "Нет прогноза" if row.segment in {Segment.PURCHASE, Segment.CONDITIONAL} else None


def _coverage_cell(row: Recommendation) -> float | str:
    if row.stock_until is not None and row.coverage_months is not None:
        return round(row.coverage_months, 1)
    return coverage_text(row)


def _sku_order_date(row: Recommendation, deadline: date | None, as_of: date) -> str | None:
    return order_date_label(deadline, as_of, below_reorder_point=stock_below_reorder_point(row)) if deadline else None


def write_report(
    facts: PlanFacts, path: str | Path, *, run_date: date | None = None,
    parameters: Mapping[str, str | int | float] | None = None,
    sku_labels: Mapping[str, str] | None = None,
) -> None:
    if run_date is None:
        run_date = date.today()
    labels = sku_labels or {}

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Заказ по поставщикам"
    cutoff_label = f"{facts.cutoff:%d.%m.%Y}"
    sheet.append(("Данные ERP на", cutoff_label))
    status = (
        f"{STALE_EXPORT_STATUS}. Перед заказом загрузите свежую выгрузку ERP."
        if (run_date - facts.cutoff).days > STALE_EXPORT_DAYS else None
    )
    sheet.append(("Статус", status))
    if facts.cutoff_source:
        sheet.append(("Как определена дата данных", facts.cutoff_source))
    if facts.supply_warning:
        sheet.append(("Предупреждение", facts.supply_warning))
    if facts.backtest_warning:
        sheet.append(("Предупреждение", facts.backtest_warning))
    headers = (
        "Поставщик", "SKU", "Товар", "К заказу", "Исключено дефицитных месяцев",
        "Прогноз на горизонт, шт.",
        "Мало данных", "Плечо, дней", "Источник плеча", "Цикл, дней",
        "Остаток на дату расчёта", "Дата прихода нового заказа", "Остаток на дату прихода",
        "В пути по месяцам", "Заказы у поставщика", "Флаги заказов", "Флаги поставщика", "Пора заказывать у поставщика",
        "Что делать", "Новинка", "Наценка за 12 мес, %", "Продажи за 12 мес, шт.",
        "Оценка спроса в год, шт.", "Флаги SKU",
        "Целевой запас", "Страховой запас", "Тип спроса", "К заказу до округления, шт.", "Кратность",
        "Покрытие, мес", "Хватит до", "Заказать не позже", "Дата заказа у поставщика",
        "Обычное пополнение, шт.", "Сезонный заказ, шт.", "Крайний срок сезонной партии",
        "Размещение обычной партии",
    )
    sheet.append((*headers, f"{Action.DECIDE}: почему"))
    confirmation_column = sheet.cell(row=sheet.max_row, column=len(headers) + 1).column_letter
    sheet.column_dimensions[confirmation_column].width = 26
    summaries = {supplier.supplier_id: supplier for supplier in facts.suppliers}
    forecast_sheet = workbook.create_sheet("Прогноз")
    forecast_sheet.append((
        "Поставщик", "SKU", "Товар", *(f"Прогноз {month:%Y-%m}" for month in facts.forecast_months),
        "Покрытие, мес", "Хватит до", "Заказать не позже",
    ))
    action_headers = ("Почему", "Что сделать", *headers)
    action_sheets: dict[Action, Worksheet] = {}
    for group in (Action.DECIDE, Action.SKIP):
        action_sheets[group] = workbook.create_sheet(group)
        action_sheets[group].append(action_headers)
        action_sheets[group].column_dimensions["A"].width = 60
        action_sheets[group].column_dimensions["B"].width = 45
        action_sheets[group].column_dimensions["E"].width = 48
        action_sheets[group].freeze_panes = "C2"
    for sku_sheet in (sheet, forecast_sheet):
        sku_sheet.column_dimensions["C"].width = 48
    orders = supplier_orders(facts)
    supplier_dates = {}
    due_suppliers = set()
    for supplier, group, _ in orders:
        summary = summaries.get(supplier)
        batches = [
            batch for row in group
            for batch in order_batches(row, summary.next_order_date if summary else None, facts.as_of)
        ]
        if supplier and batches:
            deadline = min(batch.deadline for batch in batches)
            if deadline <= facts.as_of:
                due_suppliers.add(supplier)
            supplier_dates[supplier] = order_date_label(
                deadline, facts.as_of,
                below_reorder_point=any(
                    row.order_by is not None and row.order_by <= facts.as_of and stock_below_reorder_point(row)
                    for row in group
                ),
            )

    def append_sku(target: Worksheet, row: Recommendation) -> None:
        summary = summaries.get(row.supplier_id)
        timing = summary.timing if summary else None
        if facts.typical_timing is not None and any(flag.startswith(TYPICAL_TIMING_FLAG) for flag in row.sku_flags):
            timing = facts.typical_timing
        season = row.seasonal_order
        regular_placement = row.order_by if row.grouped_season_deadline else row.regular_order_date
        transit = ", ".join(f"{month:%Y-%m} — {round(quantity)}" for month, quantity in row.in_transit_by_month)
        if sum(round(qty) for _, qty in row.in_transit_by_month) != round(row.in_transit):
            transit += f" · итого {round(row.in_transit)} шт."
        values = (
            row.supplier_id or "Поставщик не указан", row.sku_id, labels.get(row.sku_id) or None,
            displayed_order_qty(row), row.excluded_stockout_months,
            round(fsum(row.monthly_forecast)), "Да" if row.low_data else None,
            timing.lead_days if timing else None, timing.lead_source if timing else None,
            timing.cycle_days if timing else None, round(row.stock_at_calculation),
            row.arrival_date.isoformat() if row.arrival_date else None,
            row.stock_at_arrival,
            transit or None, open_orders_text(row, facts.as_of) if row.open_orders else None,
            ", ".join(row.order_flags) or None,
            (", ".join(timing.flags) or None) if timing else None,
            ("Да" if row.supplier_id in due_suppliers else "Нет") if row.supplier_id else None,
            action(row), "Да" if row.is_new else None,
            round(row.markup_pct_12m, 1) if row.markup_pct_12m is not None else None,
            round(row.sales_qty_12m), round(row.annual_demand_qty), ", ".join(row.sku_flags) or None,
            row.target_stock, row.safety_stock,
            row.demand_type, row.order_qty, row.order_multiple,
            _coverage_cell(row),
            _stock_until_label(row),
            _sku_order_date(row, row.order_by, facts.as_of),
            supplier_dates.get(row.supplier_id),
            rounded_quantity(row.regular_order_qty, row.order_multiple) if row.regular_order_qty else None,
            rounded_quantity(season.order_qty, row.order_multiple) if season and season.actionable else None,
            row.grouped_season_deadline.isoformat() if row.grouped_season_deadline else (
                season.deadline.isoformat() if season else None
            ),
            _sku_order_date(row, regular_placement or facts.as_of, facts.as_of) if row.regular_order_qty else None,
        )
        offset = 0
        if target is sheet:
            values += (why_text(row) if action(row) == Action.DECIDE else None,)
        else:
            values = (why_text(row), todo_text(row), *values)
            offset = 2
        target.append(values)
        for cell in target[target.max_row][offset:offset + 3]:
            cell.data_type = "s"
        for header in ("Остаток на дату прихода", "Целевой запас", "Страховой запас"):
            target.cell(target.max_row, offset + headers.index(header) + 1).number_format = "0"
        target.cell(target.max_row, offset + headers.index("Наценка за 12 мес, %") + 1).number_format = "0.0"
        target.cell(target.max_row, offset + headers.index("К заказу до округления, шт.") + 1).number_format = "0"
        target.cell(target.max_row, offset + headers.index("Покрытие, мес") + 1).number_format = "0.0"

    for supplier, group, total in orders:
        for row in group:
            append_sku(sheet, row)
        summary = summaries.get(supplier)
        sheet.append((
            supplier or "Поставщик не указан", "Итого", None, total,
            *([None] * (headers.index("Флаги поставщика") - headers.index("К заказу") - 1)),
            (", ".join(summary.timing.flags) or None) if summary else None,
            ("Да" if supplier in due_suppliers else "Нет") if supplier else None,
            *([None] * (headers.index("Дата заказа у поставщика") - headers.index("Пора заказывать у поставщика") - 1)),
            supplier_dates.get(supplier),
        ))
        sheet.cell(sheet.max_row, 1).data_type = "s"
        for cell in sheet[sheet.max_row]:
            cell.font = Font(bold=True)

    for row in sorted(facts.recommendations, key=lambda row: (row.supplier_id, row.sku_id)):
        forecast_sheet.append((
            row.supplier_id or "Поставщик не указан", row.sku_id, labels.get(row.sku_id) or None,
            *(round(qty) for qty in row.monthly_forecast),
            _coverage_cell(row),
            _stock_until_label(row), _sku_order_date(row, row.order_by, facts.as_of),
        ))
        forecast_sheet.cell(forecast_sheet.max_row, 4 + len(facts.forecast_months)).number_format = "0.0"
        for cell in forecast_sheet[forecast_sheet.max_row][:3]:
            cell.data_type = "s"
    skip_order = {reason: index for index, reason in enumerate(SkipReason)}
    for group, target in action_sheets.items():
        # Match the portal: no-sales rows by stock, price/cost rows by sales, decisions by quantity.
        for row in sorted(
            (row for row in facts.recommendations if action(row) == group),
            key=lambda row: (
                skip_order.get(skip_reason(row), -1),
                -row.stock_at_calculation if skip_reason(row) == SkipReason.NO_SALES else (
                    -row.sales_qty_12m if group == Action.SKIP else -displayed_order_qty(row)
                ),
                row.sku_id,
            ),
        ):
            append_sku(target, row)

    for target in (sheet, *action_sheets.values()):
        header_row = next(cells for cells in target if any(cell.value == "Наценка за 12 мес, %" for cell in cells))
        for cell in header_row:
            if cell.value in {"Наценка за 12 мес, %", "Продажи за 12 мес, шт."}:
                cell.comment = Comment(
                    "Период: 12 полных месяцев плюс текущий неполный месяц, по дату среза ERP. "
                    "Наценка взвешена по проданным штукам.", "Планирование закупок",
                )

    _append_inventory_sheets(workbook, facts, labels)
    _append_backtest(workbook.create_sheet("Бэктест"), facts)
    parameter_sheet = workbook.create_sheet("Параметры")
    parameter_sheet.append(("Параметр", "Значение"))
    parameter_sheet.append(("Данные ERP на", cutoff_label))
    if facts.cutoff_source:
        parameter_sheet.append(("Как определена дата данных", facts.cutoff_source))
    parameter_sheet.append(("Дата расчёта", facts.as_of.isoformat()))
    parameter_sheet.append(("Дата запуска", run_date.isoformat()))
    if parameters is None:
        parameters = run_parameters() | {"LLM_MAX_SKUS": llm_max_skus()}
    for name, value in parameters.items():
        parameter_sheet.append((name, value))
        if name in PARAMETER_NUMBER_FORMATS:
            parameter_sheet.cell(parameter_sheet.max_row, 2).number_format = PARAMETER_NUMBER_FORMATS[name]
    parameter_sheet.column_dimensions["A"].width = 60
    parameter_sheet.column_dimensions["B"].width = 115
    workbook.save(path)


def _append_inventory_sheets(workbook: Workbook, facts: PlanFacts, labels: Mapping[str, str]) -> None:
    seasonal = workbook.create_sheet("Сезонные заказы")
    seasonal.append((
        "SKU", "Товар", "Поставщик", "Категория", "Начало окна", "Конец окна", "Крайний срок",
        "Приход при заказе сегодня", "Остаток окна, мес",
        "Заказ при сезонном сервисе, шт.", "Заказ при обычном сервисе, шт.",
        "Прогноз остатка на конец сезона, шт.", "Крайний срок следующего сезона", "Решение", "Флаг",
        "Заказ при сезонном сервисе до округления, шт.", "Кратность", "Остаток к приходу, шт.",
        "Целевой запас, шт.", "Страховой запас, шт.",
        "Сезонный заказ в ближайшие 90 дней",
    ))
    excess = workbook.create_sheet("Избыток")
    excess.append(("SKU", "Товар", "Поставщик", "Категория", "Остаток", "В пути", "Заказы у поставщика", "Покрытие, мес",
                   "Ценовая группа", "Порог избытка, шт.", "Открытый заказ"))
    risks = workbook.create_sheet("Риски дефицита")
    risks.append(("SKU", "Товар", "Поставщик", "Остаток", "В пути", "Заказы у поставщика", "Хватит до",
                  "Приход при заказе сегодня", "Дней без товара", "Упущенные продажи, шт."))
    summary_sheet = workbook.create_sheet("Сводка")
    summary_sheet.append(("Показатель", "Количество"))
    summary = plan_summary(facts)
    # The same SKU counts as the portal tiles, then how each group splits.
    for tile in summary_tiles(summary):
        summary_sheet.append((tile.label, tile.count))
    summary_sheet.append((f"{Action.ORDER}, всего SKU", summary.order_total))
    summary_sheet.append((f"{Action.ORDER}: пора заказывать, шт.", summary.order_now_units))
    summary_sheet.append((f"{Action.ORDER}: пора заказывать, поставщиков", summary.order_now_suppliers))
    summary_sheet.append((f"{Action.ORDER}: позже чем через {days_text(ORDER_SOON_DAYS)}, SKU", summary.order_later))
    summary_sheet.append((f"{Action.ORDER}: заказ не нужен, SKU", summary.order_not_needed))
    for reason, count in summary.skip.items():
        summary_sheet.append((f"{Action.SKIP}: {reason.lower()}, SKU", count))
    summary_sheet.append(("Покрытие 6–12 мес, SKU", inventory_summary(facts)["Покрытие 6–12 мес"]))
    summary_sheet.append((UNCONFIRMED_ORDERS_LABEL, facts.unconfirmed_order_lines))
    summary_sheet.column_dimensions["A"].width = 60

    for row in sorted(facts.recommendations, key=lambda row: (row.category, row.supplier_id, row.sku_id)):
        season = row.seasonal_order
        if season is None:
            continue
        seasonal.append((
            row.sku_id, labels.get(row.sku_id) or None, row.supplier_id, category_name(row.category),
            season.window_start.isoformat(), season.window_end.isoformat(),
            season.deadline.isoformat(), row.arrival_date.isoformat() if row.arrival_date else None,
            round(season.months_remaining, 1), rounded_quantity(season.order_qty, row.order_multiple),
            rounded_quantity(season.ordinary_service_qty, row.order_multiple), round(season.stock_at_season_end),
            season.next_deadline.isoformat(), season.status,
            "Рассмотреть ускоренную доставку" if season.expedite else None,
            round(season.order_qty), row.order_multiple,
            round(season.stock_at_arrival), round(season.target_stock), round(season.safety_stock),
            "Да" if season_order_soon(row, facts.as_of) else "Нет",
        ))
        seasonal.cell(seasonal.max_row, 9).number_format = "0.0"
        seasonal.cell(seasonal.max_row, 4).data_type = "s"

    for row in excess_rows(facts):
        excess.append((
            row.sku_id, labels.get(row.sku_id) or None, row.supplier_id, row.category,
            round(row.stock_at_calculation), round(row.in_transit),
            open_orders_text(row, facts.as_of) if row.open_orders else None,
            _coverage_cell(row),
            row.price_decile, round(row.excess_threshold) if row.excess_threshold is not None else None,
            OrderFlag.RESCHEDULE if OrderFlag.RESCHEDULE in row.order_flags else None,
        ))
        excess.cell(excess.max_row, 8).number_format = "0.0"

    for row in stockout_rows(facts):
        risks.append((
            row.sku_id, labels.get(row.sku_id) or None, row.supplier_id,
            round(row.stock_at_calculation), round(row.in_transit),
            open_orders_text(row, facts.as_of) if row.open_orders else None,
            row.stock_until.isoformat() if row.stock_until else None,
            row.arrival_date.isoformat() if row.arrival_date else None,
            row.stockout_days, round(row.lost_sales),
        ))
    for sheet in (seasonal, excess, risks):
        sheet.freeze_panes = "D2"
        sheet.auto_filter.ref = sheet.dimensions
        for cells in sheet.iter_rows(min_row=2, max_col=3):
            for cell in cells:
                cell.data_type = "s"
        for cell in sheet[1]:
            sheet.column_dimensions[cell.column_letter].width = max(15, len(str(cell.value)) + 2)
        sheet.column_dimensions["B"].width = 48


def _append_backtest(sheet: Worksheet, facts: PlanFacts) -> None:
    if facts.backtest_warning:
        sheet.append((facts.backtest_warning,))
    result = facts.backtest
    if result is None:
        return
    sheet.append(("Данные бэктеста на", result.cutoff.isoformat()))
    sheet.append(("Точки с оценёнными окнами", ", ".join(f"{origin:%Y-%m}" for origin in result.origins)))
    sheet.append(METRIC_HEADERS)
    for row in result.metrics:
        sheet.append((row.breakdown, row.group, row.windows, row.wape, row.bias, row.baseline_wape, row.baseline_bias))
        for cell in sheet[sheet.max_row][3:]:
            cell.number_format = "0.0%"
    sheet.append(())
    sheet.append(("Тип спроса", "Окон с отношением", "p75", "p85", "p95", "Множитель", "Множитель для сезонной категории"))
    for group, values in result.quantiles.items():
        sheet.append((
            group, values.windows, values.p75, values.p85, values.p95,
            stock_multiplier(result, group, False), stock_multiplier(result, group, True),
        ))
        for cell in sheet[sheet.max_row][2:]:
            cell.number_format = "0.00"
    for column, width in zip("ABCDEFG", (22, 27, 10, 14, 14, 29, 37)):
        sheet.column_dimensions[column].width = width
