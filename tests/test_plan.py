import csv
import json
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from openpyxl import load_workbook

from purchase_planning import ExportValidationError, plan, write_report
from purchase_planning.facts import Segment
from purchase_planning.runs import run_plan


@pytest.fixture
def export(tmp_path):
    tables = {
        "sku_dim": (
            "sku_id,category,brand_id,supplier_id,supply_type,uom,weight_kg,warranty_months,"
            "target_cover_days,price_decile,first_sale_month,last_sale_month,months_with_sales,"
            "qty_total,avg_markup_pct_12m,on_order_qty,created\n"
            "SKU-1,tools,BRD-1,SUP-1,import,pcs,,0,0,1,2026-06,2026-08,3,920,30,0,2026-06\n"
        ),
        "sales_monthly": (
            "sku_id,category,month,qty_sold,qty_returned,orders_cnt,markup_pct,partial_month\n"
            "SKU-1,tools,2026-06,10,0,1,30,0\n"
            "SKU-1,tools,2026-07,30,0,1,30,0\n"
            "SKU-1,tools,2026-08,880,0,1,30,1\n"
        ),
        "stock_monthly": "sku_id,month,stock_qty\nSKU-1,2026-07,15\nSKU-1,2026-08,25\n",
        "purchase_orders": (
            "po_id,sku_id,supplier_id,supply_type,order_date,promised_date,planned_lead_days,"
            "status,qty_ordered,qty_confirmed,qty_received,qty_pending\n"
            "PO-1,SKU-1,SUP-1,import,2026-01-01,2026-07-01,181,completed,10,10,10,0\n"
        ),
        "receipts": (
            "sku_id,receipt_date,qty,supplier_id,supply_type\n"
            "SKU-1,2026-06-01,10,SUP-1,import\nSKU-1,2026-07-01,10,SUP-1,import\n"
        ),
        "supplier_dim": (
            "supplier_id,supply_type,receipts_cnt,skus,median_days_between_receipts,"
            "median_planned_lead_days\nSUP-1,import,1,1,,181\n"
        ),
    }
    for name, contents in tables.items():
        (tmp_path / f"{name}.csv").write_text(contents, encoding="utf-8")
    (tmp_path / "export_meta.json").write_text('{"cutoff_date":"2026-08-13"}', encoding="utf-8")
    return tmp_path


def test_forecast_starts_after_cutoff_and_uses_actual_sales_and_receipts_through_cutoff(export):
    sales = export / "sales_monthly.csv"
    sales.write_text(
        sales.read_text(encoding="utf-8").replace(",880,", ",20,"),
        encoding="utf-8",
    )
    with (export / "receipts.csv").open("a", encoding="utf-8") as source:
        source.write("SKU-1,2026-08-10,10,SUP-1,import\n")

    facts = plan(export)
    supplier = facts.suppliers[0]
    row = facts.recommendations[0]
    assert supplier.timing.lead_days == 201
    assert supplier.timing.cycle_days == 35
    assert row.arrival_date == date(2027, 3, 2)
    assert facts.forecast_months[0] == date(2026, 8, 1)
    assert facts.forecast_months[-1] == date(2027, 4, 1)
    assert row.monthly_forecast[0] == pytest.approx(20 * 18 / 31)
    assert row.monthly_forecast[-1] == pytest.approx(20 * 6 / 30)
    assert row.stock_at_calculation == 15
    assert row.stock_at_arrival == 0


@pytest.mark.parametrize(
    ("count", "lead", "source"),
    [(4, 200, "Вся история заказов + 20 дней"), (5, 210, "История заказов + p75")],
)
def test_supplier_timing_counts_batches_and_does_not_cap_lead(export, count, lead, source):
    orders = export / "purchase_orders.csv"
    header = orders.read_text(encoding="utf-8").splitlines()[0]
    starts = [date(2025, month, 1) for month in (1, 2, 3, 4, 6)]
    with orders.open("w", encoding="utf-8") as po, (export / "receipts.csv").open("a", encoding="utf-8") as receipts:
        po.write(header + "\n")
        for i, (start, promise, delay) in enumerate(
            zip(starts, [170, 180, 190, 195, 200], [0, 5, 10, 20, 40], strict=True)
        ):
            if i == count:
                break
            promised = start + timedelta(days=promise)
            received = promised + timedelta(days=delay)
            qty = 101 + i
            po.write(f"PO-{i},SKU-1,SUP-1,import,{start},{promised},{promise},completed,{qty},{qty},{qty},0\n")
            receipts.write(f"SKU-1,{received},{qty},SUP-1,import\n")
        po.write("PO-0,SKU-1,SUP-1,import,2025-01-01,2025-06-20,170,closed,100,0,0,100\n")
        receipts.write("SKU-1,2025-10-01,100,SUP-1,import\n")

    timing = plan(export).suppliers[0].timing
    assert (timing.lead_days, timing.lead_source, timing.cycle_days) == (lead, source, 31)


def test_sparse_recent_history_uses_all_planned_lead_days(export):
    orders = export / "purchase_orders.csv"
    header = orders.read_text(encoding="utf-8").splitlines()[0]
    orders.write_text(
        header + "\n"
        "PO-1,SKU-1,SUP-1,import,2024-01-01,2024-02-01,120,closed,10,0,0,10\n"
        "PO-2,SKU-1,SUP-1,import,2024-06-01,2024-07-01,150,closed,10,0,0,10\n"
        "PO-3,SKU-1,SUP-1,import,2025-01-01,2025-01-07,6,closed,10,0,0,10\n",
        encoding="utf-8",
    )

    timing = plan(export).suppliers[0].timing
    assert (timing.lead_days, timing.lead_source) == (140, "Вся история заказов + 20 дней")


@pytest.mark.parametrize(
    ("supply_type", "planned_days", "lead", "source"),
    [
        ("import", "0", 20, "Вся история заказов + 20 дней · проверить плечо"),
        ("import", "10", 30, "Вся история заказов + 20 дней"),
        ("local", "0", 20, "Вся история заказов + 20 дней"),
        ("import", "", 20, "Справочник + 20 дней · проверить плечо"),
    ],
)
def test_short_import_lead_is_flagged_in_source_column(export, supply_type, planned_days, lead, source):
    orders = export / "purchase_orders.csv"
    orders.write_text(
        orders.read_text(encoding="utf-8").replace(",181,completed", f",{planned_days},completed"),
        encoding="utf-8",
    )
    suppliers = export / "supplier_dim.csv"
    suppliers.write_text(
        suppliers.read_text(encoding="utf-8").replace(",import,1,1,,181", f",{supply_type},1,1,,0"),
        encoding="utf-8",
    )

    path = export / "order.xlsx"
    write_report(plan(export), path)
    workbook = load_workbook(path)
    try:
        header, row, _ = list(workbook["Заказ по поставщикам"].values)[-3:]
        assert row[header.index("Плечо, дней")] == lead
        assert row[header.index("Источник плеча")] == source
    finally:
        workbook.close()


def test_open_orders_respect_arrival_and_overdue_boundaries(export):
    with (export / "purchase_orders.csv").open("a", encoding="utf-8") as source:
        source.write(
            "PO-2,SKU-1,SUP-1,import,2026-01-01,2026-07-14,194,open,20,20,15,5\n"
            "PO-3,SKU-1,SUP-1,import,2026-01-01,2026-07-13,193,open,100,100,90,10\n"
            "PO-4,SKU-1,SUP-1,import,2026-01-01,2026-07-13,193,open,100,100,89,11\n"
            "PO-5,SKU-1,SUP-1,import,2026-01-01,2026-10-01,273,open,1000,1000,0,1000\n"
            "PO-6,SKU-1,SUP-1,import,2026-01-01,2026-08-14,225,closed,1000,0,0,1000\n"
        )

    facts = plan(export)
    row = facts.recommendations[0]
    assert row.in_transit_by_month == ((date(2026, 8, 1), 5), (date(2026, 10, 1), 1000))
    assert set(row.order_flags) == {
        "Хвост заказа", "Зависший заказ", "Дата прихода просроченного заказа оценена",
    }
    assert row.order_qty == 0
    assert row.arrival_date == date(2027, 3, 15)
    assert row.stock_at_arrival == pytest.approx(1000 - 20 * (5 + 15 / 31))
    assert not facts.suppliers[0].time_to_order
    assert row.stockout_risk


def test_missing_cycle_flags_supplier_and_suppresses_order(export):
    (export / "receipts.csv").write_text(
        "sku_id,receipt_date,qty,supplier_id,supply_type\nSKU-1,2026-07-01,10,SUP-1,import\n",
        encoding="utf-8",
    )

    facts = plan(export)
    supplier = facts.suppliers[0]
    assert supplier.timing.cycle_days is None
    assert "Нет истории для цикла" in supplier.timing.flags
    assert facts.recommendations[0].stock_at_arrival == 0
    assert supplier.order_qty == facts.recommendations[0].order_qty == 0
    assert not supplier.time_to_order


@pytest.mark.parametrize(("order_date", "original", "bounded"), [
    ("2025-01-01", 365, 120),
    ("2026-01-08", 7, 30),
])
def test_supplier_cycle_is_bounded(export, order_date, original, bounded):
    with (export / "purchase_orders.csv").open("a", encoding="utf-8") as source:
        source.write(f"PO-2,SKU-1,SUP-1,import,{order_date},,181,completed,10,10,10,0\n")

    timing = plan(export).suppliers[0].timing
    assert timing.cycle_days == bounded
    assert f"цикл ограничен (было {original} дней)" in timing.flags


def test_export_to_order_sheet(export):
    facts = plan(export)
    assert facts == plan(export)
    assert facts.as_of == facts.cutoff == date(2026, 8, 13)
    assert len(facts.recommendations) == 1
    row = facts.recommendations[0]
    assert (row.supplier_id, row.sku_id) == ("SUP-1", "SKU-1")
    assert row.order_qty == pytest.approx(20 * (18 / 31 + 7 + 1 / 30))

    path = export / "order.xlsx"
    write_report(facts, path, run_date=date(2026, 8, 14))
    workbook = load_workbook(path)
    try:
        header, values, total = list(workbook["Заказ по поставщикам"].values)[-3:]
        assert header[3:6] == ("Исключено дефицитных месяцев", "Прогноз на горизонт, шт.", "Мало данных")
        assert values[:2] == (row.supplier_id, row.sku_id)
        assert values[2:5] == (160, 0, round(sum(row.monthly_forecast)))
        assert values[header.index("Целевой запас")] == pytest.approx(152.2795699)
        assert values[header.index("Страховой запас")] == 0
        assert values[header.index("Тип спроса")] == "Регулярный"
        assert values[6:9] == (201, "Вся история заказов + 20 дней", 30)
        sales_column = header.index("Продажи за 12 мес, шт.")
        assert header[sales_column + 1] == "Оценка спроса в год, шт."
        assert values[sales_column:sales_column + 2] == (920, 240)
        forecast_header, forecast_row = list(workbook["Прогноз"].values)
        assert forecast_header[2:] == tuple(f"Прогноз {month:%Y-%m}" for month in facts.forecast_months)
        assert forecast_row[2:] == tuple(round(qty) for qty in row.monthly_forecast)
        assert total[:2] == ("SUP-1", "Итого")
        assert total[2] == 160
        assert total[header.index("Пора заказывать у поставщика")] == "Да"
    finally:
        workbook.close()


def test_saved_backtest_excludes_stockouts_and_calibrates_the_order(export):
    (export / "export_meta.json").write_text('{"cutoff_date":"2026-10-01"}', encoding="utf-8")
    sales = export / "sales_monthly.csv"
    sales.write_text(sales.read_text(encoding="utf-8").replace("880,0,1,30,1", "40,0,1,30,0"), encoding="utf-8")
    orders = export / "purchase_orders.csv"
    header = orders.read_text(encoding="utf-8").splitlines()[0]
    orders.write_text(
        header + "\n"
        "PO-1,SKU-1,SUP-1,import,2026-01-01,,-20,completed,100,100,100,0\n"
        "PO-2,SKU-1,SUP-1,import,2026-01-31,,-20,completed,50,50,50,0\n",
        encoding="utf-8",
    )
    june = plan(export, date(2026, 6, 30))
    assert june.recommendations[0].demand_type == "Прерывистый"
    assert june.recommendations[0].monthly_forecast == pytest.approx((10 * 30 / 31,))
    assert plan(export).recommendations[0].target_stock == pytest.approx(25.8064516)

    subprocess.run([sys.executable, "-m", "purchase_planning.backtest", str(export)], check=True)
    assert plan(export, date(2026, 6, 30)).recommendations == june.recommendations
    run = run_plan(export, export / "history")
    workbook = load_workbook(run / "report.xlsx")
    try:
        metrics = list(workbook["Бэктест"].values)
        assert metrics[1][:2] == ("Точки с оценёнными окнами", "2026-06, 2026-07")
        total = next(row for row in metrics if row[:2] == ("Все", "Все"))
        assert total[2:5] == pytest.approx((2, 4 / 7, -4 / 7))
        quantiles = {row[0]: row[1:] for row in metrics if row[0] in {"Регулярный", "Прерывистый"}}
        assert quantiles["Прерывистый"] == pytest.approx((1, 3, 3, 3, 2, 2))
        assert quantiles["Регулярный"] == pytest.approx((1, 2, 2, 2, 2, 2))
        headers, row, total = list(workbook["Заказ по поставщикам"].values)[-3:]
        assert row[headers.index("Целевой запас")] == pytest.approx(51.6129032)
        assert row[headers.index("Страховой запас")] == pytest.approx(25.8064516)
        assert row[headers.index("Кратность")] == 50
        assert row[headers.index("К заказу до округления, шт.")] == pytest.approx(51.6129032)
        assert row[2] == total[2] == 100
    finally:
        workbook.close()
    assert "57.14%" in (run / "index.html").read_text(encoding="utf-8")


def test_missing_metadata_uses_last_complete_month_and_warns_in_report(export):
    (export / "export_meta.json").unlink()
    facts = plan(export)
    assert facts.as_of == facts.cutoff == date(2026, 7, 31)
    assert facts.recommendations[0].order_qty == pytest.approx(20 * (7 + 19 / 31))

    path = export / "order.xlsx"
    write_report(facts, path)
    workbook = load_workbook(path)
    try:
        assert "Нет export_meta.json" in workbook["Заказ по поставщикам"]["B3"].value
    finally:
        workbook.close()


def test_partially_marked_month_is_excluded_from_fallback(export):
    (export / "export_meta.json").unlink()
    with (export / "sales_monthly.csv").open("a", encoding="utf-8") as source:
        source.write("SKU-1,tools,2026-08,20,0,1,30,0\n")

    assert plan(export).cutoff == date(2026, 7, 31)


def test_csv_with_only_blank_fields_is_rejected(export):
    table = export / "supplier_dim.csv"
    header = table.read_text(encoding="utf-8").splitlines()[0]
    table.write_text(f"{header}\n,,,,,\n", encoding="utf-8")

    with pytest.raises(ExportValidationError, match="supplier_dim.csv: нет строк данных"):
        plan(export)


def test_explicit_as_of_uses_only_data_available_on_that_date(export):
    facts = plan(export, as_of=date(2026, 7, 15))
    assert facts.cutoff == date(2026, 8, 13)
    assert facts.as_of == date(2026, 7, 15)
    row = facts.recommendations[0]
    assert row.stock_at_calculation == pytest.approx(25 - 10 * 15 / 31)
    assert row.monthly_forecast[0] == pytest.approx(10 * 16 / 31)
    assert facts.supply_warning


def test_demand_level_uses_complete_months_with_stock_or_sales_from_2024(export):
    with (export / "sales_monthly.csv").open("a", encoding="utf-8") as source:
        for month in range(1, 13):
            source.write(f"SKU-1,tools,2023-{month:02},900,0,1,30,0\n")
    with (export / "stock_monthly.csv").open("a", encoding="utf-8") as source:
        source.write("SKU-1,2026-05,15\n")

    row = plan(export).recommendations[0]

    # May had stock and no sales. June had sales and no opening stock.
    assert row.monthly_forecast[1] == pytest.approx(40 / 3)
    assert row.excluded_stockout_months == 28


def test_category_season_windows_on_pilot_export():
    facts = plan(Path(__file__).resolve().parents[1])
    profiles = {profile.category: profile for profile in facts.category_profiles}

    assert profiles["space_heaters"].is_seasonal
    assert profiles["space_heaters"].season_window == (9, 10, 11, 12)
    assert profiles["string_trimmers"].is_seasonal
    assert profiles["string_trimmers"].season_window == (3, 4, 5, 6, 7)
    assert not profiles["sanitary_faucets"].is_seasonal
    assert profiles["sanitary_faucets"].season_window == ()
    assert profiles["sanitary_faucets"].weights[0] == pytest.approx(0.105, abs=0.001)


def test_category_profile_uses_only_complete_months_available_on_calculation_date(export):
    (export / "export_meta.json").write_text('{"cutoff_date":"2025-12-13"}', encoding="utf-8")
    with (export / "sales_monthly.csv").open("a", encoding="utf-8") as source:
        source.write(
            "SKU-1,tools,2024-05,10,0,1,30,0\n"
            "SKU-1,tools,2025-09,30,0,1,30,0\n"
            "SKU-1,tools,2025-12,1000,0,1,30,1\n"
        )

    current = plan(export).category_profiles[0]
    historical = plan(export, as_of=date(2024, 12, 31)).category_profiles[0]

    assert current.weight_for(9) == pytest.approx(0.75)
    assert current.weight_for(12) == 0
    assert historical.weight_for(5) == 1
    assert historical.weight_for(9) == 0


@pytest.mark.parametrize(
    ("sales_months", "window"),
    [
        ([1, 2, 5, 11, 12], (11, 12, 1, 2)),
        ([1, 2, 3, 4, 5], (1, 2, 3, 4)),
        ([1, 2, 3, 6, 8, 10], (1, 2, 3, 4, 5, 6, 7, 8)),
    ],
)
def test_season_window_wraps_the_year_and_breaks_equal_ties(export, sales_months, window):
    with (export / "sales_monthly.csv").open("a", encoding="utf-8") as source:
        for month in sales_months:
            source.write(f"SKU-1,tools,2023-{month:02},20,0,1,30,0\n")

    assert plan(export).category_profiles[0].season_window == window


def test_season_window_includes_exactly_eighty_percent_with_fractional_sales(export):
    with (export / "sales_monthly.csv").open("a", encoding="utf-8") as source:
        source.write(
            "SKU-1,tools,2023-01,0.1,0,1,30,0\n"
            "SKU-1,tools,2023-02,0.7,0,1,30,0\n"
            "SKU-1,tools,2023-06,0.2,0,1,30,0\n"
        )

    assert plan(export).category_profiles[0].season_window == (1, 2)


@pytest.mark.parametrize(
    ("history", "september_forecast", "low_data"),
    [([(6, 20), (7, 30), (9, 50)], 40, False), ([(9, 100)], 240, True)],
)
def test_monthly_forecast_uses_category_weights(export, history, september_forecast, low_data):
    with (export / "sales_monthly.csv").open("a", encoding="utf-8") as source:
        for month, quantity in history:
            source.write(f"SKU-1,tools,2023-{month:02},{quantity},0,1,30,0\n")

    facts = plan(export)
    row = facts.recommendations[0]

    september = facts.forecast_months.index(date(2026, 9, 1))
    assert row.monthly_forecast[september] == pytest.approx(september_forecast)
    assert sum(row.monthly_forecast) == pytest.approx(september_forecast)
    assert row.order_qty == pytest.approx(september_forecast)
    assert row.low_data is low_data


@pytest.mark.parametrize(
    ("months_with_stock", "partial_sale", "monthly_forecast", "low_data"),
    [
        (0, False, 60, True), (1, False, 0, True), (2, False, 0, True),
        (3, False, 0, False), (0, True, 0, False),
    ],
)
def test_analog_forecast_uses_category_and_price_decile_and_respects_own_level(
    export, months_with_stock, partial_sale, monthly_forecast, low_data
):
    sku_dim = export / "sku_dim.csv"
    with sku_dim.open(encoding="utf-8", newline="") as source:
        template = next(csv.DictReader(source))
    with sku_dim.open("a", encoding="utf-8", newline="") as source:
        writer = csv.DictWriter(source, fieldnames=list(template))
        for sku, category, decile in (
            ("SKU-2", "tools", "1.0"),
            ("SKU-3", "tools", "2"),
            ("SKU-4", "tools", "1"),
            ("SKU-5", "other", "1"),
        ):
            writer.writerow({
                **template, "sku_id": sku, "category": category, "price_decile": decile,
                "first_sale_month": "", "last_sale_month": "", "months_with_sales": "0",
                "qty_total": "0",
            })
    with (export / "sales_monthly.csv").open("a", encoding="utf-8") as source:
        source.write(
            "SKU-2,tools,2026-06,100,0,1,30,0\n"
            "SKU-3,tools,2026-06,500,0,1,30,0\n"
            "SKU-5,other,2026-06,1000,0,1,30,0\n"
        )
        if partial_sale:
            source.write("SKU-4,tools,2026-08,10,0,1,30,1\n")
    with (export / "stock_monthly.csv").open("a", encoding="utf-8") as source:
        for month in range(5, 5 + months_with_stock):
            source.write(f"SKU-4,2026-{month:02},15\n")

    rows = {row.sku_id: row for row in plan(export).recommendations}

    assert rows["SKU-4"].monthly_forecast == pytest.approx(
        (monthly_forecast * 18 / 31, *((monthly_forecast,) * 7), monthly_forecast / 30)
    )
    assert rows["SKU-4"].low_data is low_data
    assert not rows["SKU-2"].low_data
    assert rows["SKU-4"].segment == (Segment.DEAD if months_with_stock else Segment.CONDITIONAL)
    assert rows["SKU-4"].is_new is partial_sale


def test_malformed_metadata_is_rejected(export):
    (export / "export_meta.json").write_text('{"cutoff_date":"2026-08"}', encoding="utf-8")
    with pytest.raises(ExportValidationError, match="cutoff_date"):
        plan(export)


def test_rejects_invalid_export_with_all_reasons_and_no_report(export):
    sales = export / "sales_monthly.csv"
    with sales.open(encoding="utf-8", newline="") as source:
        rows = list(csv.reader(source))
    rows[0].remove("partial_month")
    with sales.open("w", encoding="utf-8", newline="") as destination:
        csv.writer(destination).writerows(rows)
    stock = export / "stock_monthly.csv"
    stock.write_text(stock.read_text(encoding="utf-8").replace(",", ";"), encoding="utf-8")
    (export / "purchase_orders.csv").write_bytes(b"\xff")
    receipts = export / "receipts.csv"
    receipts.write_text(
        receipts.read_text(encoding="utf-8").replace("SKU-1", "SKU-999"), encoding="utf-8"
    )
    (export / "supplier_dim.csv").write_text("", encoding="utf-8")
    path = export / "order.xlsx"

    with pytest.raises(ExportValidationError) as rejected:
        write_report(plan(export), path)

    reasons = "\n".join(rejected.value.reasons)
    assert len(rejected.value.reasons) == 5
    assert "sales_monthly.csv: отсутствуют обязательные колонки partial_month" in reasons
    assert "stock_monthly.csv:" in reasons and "нужна запятая" in reasons
    assert "purchase_orders.csv: неверная кодировка" in reasons
    assert "receipts.csv: sku_id вне справочника: 'SKU-999'" in reasons
    assert "supplier_dim.csv: файл пуст" in reasons
    assert not path.exists()


def test_sleeping_sku_is_reported_without_an_order(export):
    sales = export / "sales_monthly.csv"
    header = sales.read_text(encoding="utf-8").splitlines()[0]
    sales.write_text(header + "\nSKU-1,tools,2024-07,10,0,1,30,0\n", encoding="utf-8")
    (export / "stock_monthly.csv").write_text(
        "sku_id,month,stock_qty\nSKU-1,2024-07,15\n", encoding="utf-8",
    )

    receipts = export / "receipts.csv"
    receipts.write_text(receipts.read_text(encoding="utf-8").replace("2026-", "2024-"), encoding="utf-8")

    facts = plan(export)
    row, = facts.recommendations
    assert row.segment == "Спящие"
    assert row.order_qty == 0
    assert row.excluded_stockout_months == 24
    assert not facts.suppliers[0].time_to_order

    path = export / "order.xlsx"
    write_report(facts, path)
    with path.open("rb") as report:
        workbook = load_workbook(report)
        assert list(workbook["Спящие"].values)[1][:3] == ("SUP-1", "SKU-1", 0)
        assert all("SKU-1" not in values for values in workbook["Заказ по поставщикам"].values)
        workbook.close()


@pytest.mark.parametrize(
    ("markup", "prior_markup", "previous", "segment", "can_order"),
    [
        ("19.99", "35", None, "На пересмотр", False),
        ("20", "35", None, "Условно к закупке", True),
        ("24.99", "35", None, "Условно к закупке", True),
        ("25", "35", None, "К закупке", True),
        ("27", "19", None, "На пересмотр", False),
        ("27", "19", "К закупке", "К закупке", True),
        ("29", "35", "На пересмотр", "На пересмотр", False),
        ("30", "24", "На пересмотр", "К закупке", True),
        ("", "35", None, "На пересмотр", False),
    ],
)
def test_segment_uses_markup_priority_and_hysteresis(export, markup, prior_markup, previous, segment, can_order):
    sales = export / "sales_monthly.csv"
    header = sales.read_text(encoding="utf-8").splitlines()[0]
    sales.write_text(
        header + "\n"
        f"SKU-1,tools,2025-06,10,0,1,{prior_markup},0\n"
        f"SKU-1,tools,2026-07,30,4,1,{markup},0\n",
        encoding="utf-8",
    )
    orders = export / "purchase_orders.csv"
    orders.write_text(
        orders.read_text(encoding="utf-8").replace(",181,completed", ",400,completed"), encoding="utf-8",
    )
    stock = export / "stock_monthly.csv"
    stock.write_text(stock.read_text(encoding="utf-8").replace("2026-08,25", "2026-08,0"), encoding="utf-8")

    previous_segments = None if previous is None else {"SKU-1": Segment(previous)}
    row, = plan(export, previous_segments=previous_segments).recommendations
    assert row.segment == segment
    assert (row.order_qty > 0) == can_order
    assert "Возвраты > 10%" in row.sku_flags
    assert ("Проверить себестоимость" in row.sku_flags) == (markup == "")
    assert not row.is_new


@pytest.mark.parametrize(
    ("missing_units", "segment"),
    [(9, Segment.PURCHASE), (10, Segment.PURCHASE), (11, Segment.REVIEW)],
)
def test_missing_cost_uses_share_of_units_and_keeps_known_markup(export, missing_units, segment):
    sales = export / "sales_monthly.csv"
    header = sales.read_text(encoding="utf-8").splitlines()[0]
    sales.write_text(
        header + "\n"
        "SKU-1,tools,2026-04,2,0,1,20,0\n"
        "SKU-1,tools,2026-05,8,0,1,40,0\n"
        f"SKU-1,tools,2026-06,{missing_units},0,1,,0\n",
        encoding="utf-8",
    )

    row, = plan(export).recommendations
    assert row.markup_pct_12m == pytest.approx(36)
    assert row.segment == segment
    assert "Проверить себестоимость" in row.sku_flags


def test_pilot_export_lists_orders_and_flags_missing_supplier_timing(tmp_path):
    directory = Path(__file__).resolve().parents[1]
    facts = plan(directory)
    with (directory / "sku_dim.csv").open(encoding="utf-8", newline="") as source:
        catalog = {row["sku_id"]: row for row in csv.DictReader(source)}
    rows = {row.sku_id: row for row in facts.recommendations}
    assert len(facts.recommendations) == len(rows) == len(catalog) == 791
    assert rows.keys() == catalog.keys()
    unknown = [row for row in rows.values() if catalog[row.sku_id]["supply_type"] == "unknown"]
    assert len(unknown) == 153
    assert all(row.order_qty == 0 and "Поставщик не указан" in row.sku_flags for row in unknown)
    missing_lead = {summary.supplier_id for summary in facts.suppliers if summary.timing.lead_days is None}
    assert missing_lead
    assert all(
        row.order_qty == 0 and "Не определено плечо поставки" in row.sku_flags
        for row in rows.values() if row.supplier_id in missing_lead
    )

    path = tmp_path / "order.xlsx"
    write_report(facts, path)
    workbook = load_workbook(path, read_only=True)
    try:
        for segment in Segment:
            name = "Заказ по поставщикам" if segment == Segment.PURCHASE else segment
            data = [row for row in workbook[name].values if row[1] in catalog]
            assert sorted(row[1] for row in data) == sorted(
                row.sku_id for row in rows.values()
                if (row.order_qty > 0 if segment == Segment.PURCHASE else row.segment == segment)
            )
            if segment == Segment.REVIEW:
                header = next(workbook[name].values)
                sales = [row[header.index("Продажи за 12 мес, шт.")] for row in data]
                assert sales == sorted(sales, reverse=True)
            if segment in {Segment.DEAD, Segment.SLEEPING, Segment.REVIEW}:
                assert all(row[2] == 0 for row in data)
    finally:
        workbook.close()

def test_same_export_produces_identical_facts(export):
    assert plan(export) == plan(export)


def test_run_history_compares_the_same_export_and_preserves_parameters(export, tmp_path):
    history = tmp_path / "runs"
    first = run_plan(export, history)
    second = run_plan(export, history)
    assert first != second
    assert "Первый запуск" in (first / "index.html").read_text(encoding="utf-8")
    assert "Изменений нет" in (second / "index.html").read_text(encoding="utf-8")
    assert (first / "facts.json").read_bytes() == (second / "facts.json").read_bytes()
    assert json.loads((first / "diff.json").read_text(encoding="utf-8"))["previous_run"] is None
    assert json.loads((second / "diff.json").read_text(encoding="utf-8")) == {
        "previous_run": first.name,
        "changes": [],
    }
    parameters = json.loads((second / "parameters.json").read_text(encoding="utf-8"))
    with (second / "report.xlsx").open("rb") as report:
        workbook = load_workbook(report, read_only=True)
        try:
            values = dict(list(workbook["Параметры"].values)[1:])
            assert values["Порог устаревания, дней"] == parameters["Порог устаревания, дней"] == 35
            assert values["Выход из пересмотра, %"] == 30
        finally:
            workbook.close()

def test_run_comparison_uses_saved_segment_and_observed_drivers(export, tmp_path):
    history = tmp_path / "runs"
    sales = export / "sales_monthly.csv"
    sales.write_text(
        sales.read_text(encoding="utf-8").replace(",880,", ",20,").replace(",30,0\n", ",18,0\n"),
        encoding="utf-8",
    )
    orders = export / "purchase_orders.csv"
    with orders.open("a", encoding="utf-8") as source:
        source.write("PO-2,SKU-1,SUP-1,import,2026-02-01,2026-08-20,200,open,10,10,0,10\n")

    first = run_plan(export, history)
    sales.write_text(sales.read_text(encoding="utf-8").replace(",18,0\n", ",27,0\n"), encoding="utf-8")
    assert plan(export).recommendations[0].segment == Segment.PURCHASE

    orders.write_text(
        orders.read_text(encoding="utf-8").replace(",open,", ",closed,"),
        encoding="utf-8",
    )
    with (export / "receipts.csv").open("a", encoding="utf-8") as source:
        source.write("SKU-1,2026-08-10,10,SUP-1,import\n")
    second = run_plan(export, history)
    facts = json.loads((second / "facts.json").read_text(encoding="utf-8"))
    assert facts["recommendations"][0]["segment"] == Segment.REVIEW

    sales.write_text(sales.read_text(encoding="utf-8").replace(",27,0\n", ",30,0\n"), encoding="utf-8")
    # Compare against the first run to exercise the same observed inputs in one change.
    direct = tmp_path / "direct"
    direct.mkdir()
    shutil.copytree(first, direct / first.name)
    third = run_plan(export, direct)
    diff = json.loads((third / "diff.json").read_text(encoding="utf-8"))
    assert diff["previous_run"] == first.name
    change, = diff["changes"]
    assert change["sku_id"] == "SKU-1"
    assert change["segment"] == [Segment.REVIEW, Segment.PURCHASE]
    assert change["order_qty"][0] == 0 < change["order_qty"][1]
    assert change["stockout_risk"] == [False, True]
    assert change["drivers"] == ["sales", "receipt", "order_cancelled"]
    assert change["flags"] == []

def test_parameter_change_explains_new_risk_and_recovery(export, tmp_path, monkeypatch):
    from purchase_planning import segments

    history = tmp_path / "runs"
    run_plan(export, history)

    monkeypatch.setattr(segments, "RECOVERY_MARKUP_PCT", 35)
    monkeypatch.setattr(segments, "REVIEW_MARKUP_PCT", 31)
    second = run_plan(export, history)
    change, = json.loads((second / "diff.json").read_text(encoding="utf-8"))["changes"]
    assert change["segment"] == [Segment.PURCHASE, Segment.REVIEW]
    assert change["stockout_risk"] == [True, False]
    assert change["drivers"] == ["parameter_changed"]

    monkeypatch.setattr(segments, "REVIEW_MARKUP_PCT", 20)
    monkeypatch.setattr(segments, "RECOVERY_MARKUP_PCT", 30)
    third = run_plan(export, history)
    change, = json.loads((third / "diff.json").read_text(encoding="utf-8"))["changes"]
    assert change["segment"] == [Segment.REVIEW, Segment.PURCHASE]
    assert change["stockout_risk"] == [False, True]
    assert change["drivers"] == ["parameter_changed"]
