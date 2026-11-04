import csv
from datetime import date, timedelta
from pathlib import Path

import pytest
from openpyxl import load_workbook

from purchase_planning import ExportValidationError, plan, write_report


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
    assert row.order_qty == pytest.approx(20 * 18 / 31 - 5 + 20)
    assert row.arrival_date == date(2027, 3, 15)
    assert row.stock_at_arrival == pytest.approx(1000 - 20 * (5 + 15 / 31))
    assert facts.suppliers[0].time_to_order


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
        assert header[3:14] == (
            "Исключено дефицитных месяцев", "Прогноз 2026-08", "Прогноз 2026-09",
            "Прогноз 2026-10", "Прогноз 2026-11", "Прогноз 2026-12", "Прогноз 2027-01",
            "Прогноз 2027-02", "Прогноз 2027-03", "Прогноз 2027-04", "Мало данных",
        )
        assert values[:2] == (row.supplier_id, row.sku_id)
        assert values[2:13] == pytest.approx((row.order_qty, 29, *row.monthly_forecast))
        assert values[14:17] == (201, "Вся история заказов + 20 дней", 30)
        assert total[:2] == ("SUP-1", "Итого")
        assert total[2] == pytest.approx(row.order_qty)
        assert total[header.index("Пора заказывать у поставщика")] == "Да"
    finally:
        workbook.close()


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
