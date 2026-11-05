import csv
import json
import os
import shutil
import subprocess
import sys
from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path

import pytest
from openpyxl import load_workbook

from purchase_planning import ExportValidationError, plan, write_report
from purchase_planning.backtest import run_backtest
from purchase_planning.dashboard import write_dashboard
from purchase_planning.actions import Action, action
from purchase_planning.facts import ESTIMATED_CUTOFF_SOURCE, TYPICAL_TIMING_FLAG, OrderFlag, Segment
from purchase_planning.order_sheet import open_orders_text
from purchase_planning.planner import calculate
from purchase_planning.runs import REJECTED_EXIT, STATUS_FILENAME, main as runs_main, run_plan


@pytest.fixture
def export(tmp_path):
    for source in (Path(__file__).parent / "fixtures" / "export").iterdir():
        shutil.copyfile(source, tmp_path / source.name)
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


def test_publication_retains_history_and_rejects_bad_exports(export, monkeypatch):
    monkeypatch.setenv("PURCHASE_CODE_REVISION", "code-sha")
    monkeypatch.setenv("GITHUB_SHA", "export-sha")
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    history = export / "runs"

    def publish(name, *options):
        return subprocess.run(
            [sys.executable, "-m", "purchase_planning.runs", str(export), "--stub-llm",
             "--history-dir", str(history), "--site-dir", str(export / name), *options],
            capture_output=True, env={**os.environ, "PYTHONUTF8": "1"},
        )

    first = publish("site-1", "--reject-duplicate")
    assert first.returncode == 0, first.stderr
    assert publish("site-retry", "--reject-duplicate").returncode == 0
    assert [path.name for path in history.iterdir() if path.is_dir()] == ["000001"]

    monkeypatch.setenv("GITHUB_RUN_ID", "2")
    duplicate = publish("site-duplicate", "--reject-duplicate")
    assert duplicate.returncode == REJECTED_EXIT
    assert json.loads((export / "site-duplicate" / STATUS_FILENAME).read_text(encoding="utf-8"))["reasons"] == [
        "Эта выгрузка уже на портале, план по ней не изменится.",
    ]

    second = publish("site-2")
    assert second.returncode == 0, second.stderr
    site = export / "site-2"
    assert 'href="000001/index.html"' in (site / "history.html").read_text(encoding="utf-8")
    assert (site / "000001" / "report.xlsx").is_file()
    page = (site / "index.html").read_text(encoding="utf-8")
    assert '<time datetime="2026-08-13">' in page
    assert "Изменений нет." in page
    assert "Пояснение LLM" not in page
    metadata = json.loads((history / "000002" / "run.json").read_text(encoding="utf-8"))
    assert (metadata["source_revision"], metadata["code_revision"]) == ("export-sha", "code-sha")
    assert len(metadata["input_sha256"]) == 64
    assert metadata["llm"]["explained_skus"] == 0
    assert metadata["llm"]["discarded_skus"] == 1
    assert metadata["llm"]["discarded_by_reason"] == {"stub": 1}
    assert "SKU с пояснением — 0, отброшено — 1 (заглушка — 1)" in second.stdout.decode("utf-8")

    monkeypatch.setenv("GITHUB_RUN_ID", "3")
    (export / "sales_monthly.csv").write_text("", encoding="utf-8")
    bad_export = publish("site-invalid", "--reject-duplicate")
    assert bad_export.returncode == REJECTED_EXIT
    assert not (history / "000003").exists()
    assert (export / "site-invalid" / "index.html").read_text(encoding="utf-8") == page
    status = json.loads((export / "site-invalid" / STATUS_FILENAME).read_text(encoding="utf-8"))
    assert (status["accepted"], status["run"], status["reasons"]) == (False, "000002", ["sales_monthly.csv: файл пуст."])


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


def test_only_open_orders_with_planned_arrival_ahead_are_in_transit(export):
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
    # Planned arrival dates that have passed are not counted, however recent: the buyer sees the delay.
    assert row.in_transit_by_month == ((date(2026, 10, 1), 1000),)
    assert {"Хвост заказа", "Опаздывает"} <= set(row.order_flags)
    assert open_orders_text(row, facts.as_of) == (
        "10 шт. — опаздывает на 31 день, в расчёт не взято; 11 шт. — опаздывает на 31 день, в расчёт не взято; "
        "5 шт. — опаздывает на 30 дней, в расчёт не взято; 1000 шт. — придёт 01.10.2026, через 49 дней"
    )
    assert row.order_qty == 0
    assert row.arrival_date == date(2027, 3, 15)
    assert row.stock_at_arrival == pytest.approx(1000 - 20 * (5 + 15 / 31))
    assert not facts.suppliers[0].time_to_order
    assert row.stockout_risk


def test_unconfirmed_open_order_lines_stay_in_transit_and_are_counted(export):
    with (export / "purchase_orders.csv").open("a", encoding="utf-8") as source:
        source.write(
            "PO-2,SKU-1,SUP-1,import,2026-06-01,2026-10-01,122,open,100,0,0,100\n"
            "PO-3,SKU-1,SUP-1,import,2026-06-01,2026-10-01,122,open,150,0,0,150\n"
            "PO-4,SKU-1,SUP-1,import,2026-06-01,2026-10-01,122,open,200,200,0,200\n"
            "PO-5,SKU-1,SUP-1,import,2026-06-01,2026-10-01,122,closed,200,0,0,200\n"
        )

    calculation = calculate(export)
    assert {line.po_id for line in calculation.orders if line.is_unconfirmed} == {"PO-2", "PO-3"}
    assert calculation.facts.unconfirmed_order_lines == 2
    row = calculation.facts.recommendations[0]
    assert OrderFlag.UNCONFIRMED in row.order_flags
    assert row.in_transit == 450


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
    assert row.order_qty == pytest.approx(20 * (29 / 31 + 1 / 30))

    path = export / "order.xlsx"
    write_report(facts, path, run_date=date(2026, 8, 14))
    workbook = load_workbook(path)
    try:
        header, values, total = list(workbook["Заказ по поставщикам"].values)[-3:]
        assert header[2:7] == (
            "Товар", "К заказу", "Исключено дефицитных месяцев", "Прогноз на горизонт, шт.", "Мало данных",
        )
        assert values[:2] == (row.supplier_id, row.sku_id)
        assert values[3:6] == (20, 0, round(sum(row.monthly_forecast)))
        assert values[header.index("Целевой запас")] == pytest.approx(19.3763441)
        assert values[header.index("Страховой запас")] == 0
        assert values[header.index("Тип спроса")] == "Регулярный"
        assert values[7:10] == (201, "Вся история заказов + 20 дней", 30)
        sales_column = header.index("Продажи за 12 мес, шт.")
        assert header[sales_column + 1] == "Оценка спроса в год, шт."
        assert values[sales_column:sales_column + 2] == (920, 240)
        forecast_header, forecast_row = list(workbook["Прогноз"].values)
        assert forecast_header[3:3 + len(facts.forecast_months)] == tuple(
            f"Прогноз {month:%Y-%m}" for month in facts.forecast_months
        )
        assert forecast_row[3:3 + len(facts.forecast_months)] == tuple(round(qty) for qty in row.monthly_forecast)
        assert forecast_row[forecast_header.index("Покрытие, мес")] == 0
        assert total[:2] == ("SUP-1", "Итого")
        assert total[header.index("К заказу")] == 20
        assert total[header.index("Пора заказывать у поставщика")] == "Да"
    finally:
        workbook.close()


def test_order_covers_cycle_and_safety_stock_at_arrival(export):
    (export / "export_meta.json").write_text('{"cutoff_date":"2026-01-01"}', encoding="utf-8")
    sales = export / "sales_monthly.csv"
    header = sales.read_text(encoding="utf-8").splitlines()[0]
    # One unit per day gives an exact 30 units per 30-day month.
    sales.write_text(
        header + "\n" + "".join(
            f"SKU-1,tools,2025-{month:02},{monthrange(2025, month)[1]},0,1,30,0\n"
            for month in range(1, 13)
        ),
        encoding="utf-8",
    )
    orders = export / "purchase_orders.csv"
    header = orders.read_text(encoding="utf-8").splitlines()[0]
    orders.write_text(
        header + "\n"
        "PO-1,SKU-1,SUP-1,import,2025-01-01,,100,completed,100,100,100,0\n"
        "PO-2,SKU-1,SUP-1,import,2025-03-02,,100,completed,100,100,100,0\n",
        encoding="utf-8",
    )
    (export / "backtest.json").write_text(json.dumps({
        "cutoff": "2026-01-01",
        "origins": [],
        "metrics": [],
        "quantiles": {"Регулярный": {"windows": 1, "p75": 1.5, "p85": 1.5, "p95": 1.5}},
    }), encoding="utf-8")

    facts = plan(export, date(2026, 1, 1))
    timing = facts.suppliers[0].timing
    row = facts.recommendations[0]
    assert (timing.lead_days, timing.cycle_days) == (120, 60)
    assert row.stock_at_calculation == row.stock_at_arrival == 0
    assert sum(row.monthly_forecast) == pytest.approx(180)
    # Nothing is in stock before arrival, so faster sales cannot eat it: the safety stock
    # protects only the 60-day cycle after arrival, (1.5 - 1) x 60.
    assert row.safety_stock == pytest.approx(30)
    assert row.target_stock == pytest.approx(90)
    assert row.order_qty == pytest.approx(90)
    assert facts.suppliers[0].order_qty == 100


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
        assert row[headers.index("К заказу")] == total[headers.index("К заказу")] == 100
    finally:
        workbook.close()
    assert "57,1%" in (run / "index.html").read_text(encoding="utf-8")


def test_backtest_scores_net_sales_without_excluding_fully_returned_month(export):
    (export / "export_meta.json").write_text('{"cutoff_date":"2026-10-01"}', encoding="utf-8")
    sales = export / "sales_monthly.csv"
    sales.write_text(
        sales.read_text(encoding="utf-8").replace("2026-07,30,0", "2026-07,30,30")
        .replace("880,0,1,30,1", "40,0,1,30,0"),
        encoding="utf-8",
    )
    (export / "stock_monthly.csv").write_text("sku_id,month,stock_qty\nSKU-1,2026-08,25\n", encoding="utf-8")
    with (export / "purchase_orders.csv").open("a", encoding="utf-8") as source:
        source.write("PO-2,SKU-1,SUP-1,import,2026-01-31,,-20,completed,50,50,50,0\n")
    orders = export / "purchase_orders.csv"
    orders.write_text(orders.read_text(encoding="utf-8").replace("2026-07-01,181", ",-20"), encoding="utf-8")

    metric = next(row for row in run_backtest(export).metrics if row.breakdown == row.group == "Все")
    # The 30-day windows compare forecasts of 10 and 5 to net sales of 0 and 40, scaled by 30/31.
    assert metric.windows == 2
    assert metric.wape == pytest.approx(45 / 40)
    assert metric.bias == pytest.approx(-25 / 40)


def test_missing_metadata_uses_last_complete_month_and_reports_source(export):
    (export / "export_meta.json").unlink()
    facts = plan(export)
    assert facts.as_of == facts.cutoff == date(2026, 7, 31)
    assert facts.recommendations[0].order_qty == pytest.approx(20 * (11 / 28 + 19 / 31))

    path = export / "order.xlsx"
    write_report(facts, path)
    workbook = load_workbook(path)
    try:
        parameters = dict(workbook["Параметры"].values)
        assert parameters["Как определена дата данных"] == "по последнему полному месяцу продаж 31.07.2026"
    finally:
        workbook.close()


def test_partially_marked_month_is_excluded_from_fallback(export):
    (export / "export_meta.json").unlink()
    with (export / "sales_monthly.csv").open("a", encoding="utf-8") as source:
        source.write("SKU-1,tools,2026-08,20,0,1,30,0\n")

    assert plan(export).cutoff == date(2026, 7, 31)


@pytest.mark.parametrize(("order_date", "metadata_date", "expected", "source", "stock"), [
    ("2026-07-01", None, date(2026, 8, 1), "по последнему приходу 01.08.2026", 70),
    ("2026-08-02", None, date(2026, 8, 2), "по последнему заказу 02.08.2026", 70),
    ("2026-07-01", "2026-07-31", date(2026, 7, 31), "по явно заданной дате выгрузки", 0),
])
def test_cutoff_uses_latest_observed_activity_unless_explicit(export, order_date, metadata_date, expected, source, stock):
    metadata = export / "export_meta.json"
    if metadata_date is None:
        metadata.unlink()
    else:
        metadata.write_text(json.dumps({"cutoff_date": metadata_date}), encoding="utf-8")
    sales = export / "sales_monthly.csv"
    sales.write_text(sales.read_text(encoding="utf-8").replace("2026-08,880,", "2026-08,0,"), encoding="utf-8")
    with (export / "receipts.csv").open("a", encoding="utf-8") as receipts:
        receipts.write("SKU-1,2026-08-01,45,SUP-1,import\n")
    with (export / "purchase_orders.csv").open("a", encoding="utf-8") as orders:
        orders.write("PO-2,SKU-1,SUP-1,import,2026-07-01,2026-08-01,31,completed,45,45,45,0\n")
        if order_date == "2026-08-02":
            orders.write("PO-3,SKU-1,SUP-1,import,2026-08-02,,31,closed,45,0,0,45\n")

    facts = plan(export)
    assert facts.cutoff == facts.as_of == expected
    assert facts.cutoff_source == source
    row = facts.recommendations[0]
    assert row.stock_at_calculation == stock
    assert row.in_transit == 0


@pytest.mark.parametrize(("partial_sales", "receipt_day", "order_day", "metadata_day", "expected_day"), [
    (26, 1, 1, None, 13),
    (26, 17, 1, None, 17),
    (26, 1, 18, None, 18),
    (0, 1, 1, None, 1),
    (100, 1, 1, None, 30),
    (26, 17, 18, 11, 11),
])
def test_cutoff_estimates_partial_month_and_respects_observed_dates(
    export, partial_sales, receipt_day, order_day, metadata_day, expected_day,
):
    metadata = export / "export_meta.json"
    if metadata_day is None:
        metadata.unlink()
    else:
        metadata.write_text(json.dumps({"cutoff_date": f"2026-08-{metadata_day:02}"}), encoding="utf-8")
    sales = export / "sales_monthly.csv"
    sales.write_text(sales.read_text(encoding="utf-8").replace(",880,", f",{partial_sales},"), encoding="utf-8")
    with sales.open("a", encoding="utf-8") as source:
        source.write(
            "SKU-1,tools,2025-06,10,0,1,30,0\n"
            "SKU-1,tools,2025-07,10,0,1,30,0\n"
            "SKU-1,tools,2025-08,31,10,1,30,0\n"
        )
    with (export / "receipts.csv").open("a", encoding="utf-8") as source:
        source.write(f"SKU-1,2026-08-{receipt_day:02},10,SUP-1,import\n")
    with (export / "purchase_orders.csv").open("a", encoding="utf-8") as source:
        source.write(f"PO-2,SKU-1,SUP-1,import,2026-08-{order_day:02},,0,closed,10,0,0,10\n")

    facts = plan(export)
    assert facts.cutoff == date(2026, 8, expected_day)
    assert facts.cutoff_is_estimated == (metadata_day is None)


def test_full_inbox_cutoff_and_open_orders_with_future_arrivals(tmp_path):
    directory = Path(__file__).resolve().parents[1] / "inbox"
    assert not (directory / "export_meta.json").exists()
    calculation = calculate(directory)
    facts = calculation.facts
    assert facts.cutoff == date(2026, 8, 13)
    future = [
        line for line in calculation.orders
        if line.status == "open" and line.promised is not None and line.promised > facts.cutoff
    ]
    assert len(future) == 22
    assert sum(line.pending for line in future) == 17_127
    assert facts.unconfirmed_order_lines == 54
    # The dataset README: 22 open lines with a future arrival date, 17 127 units in transit.
    assert sum(row.in_transit for row in facts.recommendations) == 17_127

    dashboard = tmp_path / "index.html"
    write_dashboard(facts, dashboard, run_date=date(2026, 11, 4), previous_run=None, changes=[])
    page = dashboard.read_text(encoding="utf-8")
    assert 'Остатки и продажи из ERP на <time datetime="2026-08-13">13.08.2026</time>' in page
    # The ERP rarely fills the confirmed quantity, so the count is not a headline metric.
    assert "строк заказов</dt>" not in page

    path = tmp_path / "report.xlsx"
    write_report(facts, path, run_date=date(2026, 11, 4))
    workbook = load_workbook(path, read_only=True)
    try:
        parameters = dict(workbook["Параметры"].values)
        assert parameters["Данные ERP на"] == "13.08.2026"
        assert parameters["Как определена дата данных"] == ESTIMATED_CUTOFF_SOURCE
        assert dict(workbook["Сводка"].values)["Подтверждение в ERP не заполнено, строк заказов"] == 54
    finally:
        workbook.close()


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


def test_demand_level_uses_complete_months_with_stock_or_sales_of_the_last_year(export):
    with (export / "sales_monthly.csv").open("a", encoding="utf-8") as source:
        for month in range(1, 13):
            source.write(f"SKU-1,tools,2023-{month:02},900,0,1,30,0\n")
    with (export / "stock_monthly.csv").open("a", encoding="utf-8") as source:
        source.write("SKU-1,2026-05,15\n")

    row = plan(export).recommendations[0]

    # May had stock and no sales. June had sales and no opening stock.
    assert row.monthly_forecast[1] == pytest.approx(40 / 3)
    # Only the last 12 complete months count: August 2025 to July 2026.
    assert row.excluded_stockout_months == 9


def test_net_sales_drive_demand_but_gross_sales_keep_month_and_segment(export):
    sales = export / "sales_monthly.csv"
    sales.write_text(
        sales.read_text(encoding="utf-8").replace("2026-06,10,0", "2026-06,10,20")
        .replace("2026-07,30,0", "2026-07,30,10"),
        encoding="utf-8",
    )
    with sales.open("a", encoding="utf-8") as source:
        source.write(
            "SKU-1,tools,2025-06,40,30,1,30,0\n"
            "SKU-1,tools,2025-07,50,20,1,30,0\n"
        )

    facts = plan(export)
    row = facts.recommendations[0]
    profile = facts.category_profiles[0]
    assert profile.weights[5:7] == pytest.approx((0.25, 0.75))
    # June 2025 and July 2025 are outside the 12-month window. June 2026 nets to zero.
    assert row.annual_demand_qty == pytest.approx(20)
    assert row.excluded_stockout_months == 10
    # The category sells only in June and July. Empty shelves off season are not a stockout to check,
    # and both season months had stock or sales.
    assert (row.missing_stock_months, row.checked_stock_months) == (0, 2)
    assert row.segment == Segment.PURCHASE
    assert row.sales_qty_12m == 920
    assert row.markup_pct_12m == 30


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
    assert row.order_qty == 0
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
    # A sale in the incomplete month counts for the markup, as in the ERP: 30% is known, so the SKU is ordered.
    expected = Segment.DEAD if months_with_stock else Segment.PURCHASE if partial_sale else Segment.CONDITIONAL
    assert rows["SKU-4"].segment == expected
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
    assert row.excluded_stockout_months == 12
    assert not facts.suppliers[0].time_to_order

    path = export / "order.xlsx"
    write_report(facts, path)
    with path.open("rb") as report:
        workbook = load_workbook(report)
        header, sleeping = workbook["Не заказывать"].values
        assert sleeping[:4] == (
            "Год без продаж и без остатка", "Решить, возвращать ли товар в ассортимент", "SUP-1", "SKU-1",
        )
        assert sleeping[header.index("К заказу")] == 0
        assert all("SKU-1" not in values for values in workbook["Заказ по поставщикам"].values)
        workbook.close()


@pytest.mark.parametrize(
    ("markup", "prior_markup", "segment", "can_order"),
    [
        ("19.99", "35", "На пересмотр", False),
        ("24.99", "35", "На пересмотр", False),
        ("25", "35", "К закупке", True),
        # Only the last 12 months count: a low markup a year ago does not hold the SKU back.
        ("27", "19", "К закупке", True),
        ("", "35", "На пересмотр", False),
    ],
)
def test_segment_uses_the_single_markup_threshold(export, markup, prior_markup, segment, can_order):
    sales = export / "sales_monthly.csv"
    header = sales.read_text(encoding="utf-8").splitlines()[0]
    sales.write_text(
        header + "\n"
        + "".join(f"SKU-1,tools,2025-{month:02},30,0,1,{prior_markup},0\n" for month in range(1, 8))
        + f"SKU-1,tools,2026-07,30,4,1,{markup},0\n",
        encoding="utf-8",
    )
    orders = export / "purchase_orders.csv"
    orders.write_text(
        orders.read_text(encoding="utf-8").replace(",181,completed", ",280,completed"), encoding="utf-8",
    )
    stock = export / "stock_monthly.csv"
    stock.write_text(stock.read_text(encoding="utf-8").replace("2026-08,25", "2026-08,0"), encoding="utf-8")
    with stock.open("a", encoding="utf-8") as source:
        # In stock without sales: not a long stockout that would need the buyer anyway.
        for month in ("2025-08", "2025-09", "2025-10", "2025-11", "2025-12", *(f"2026-{m:02}" for m in range(1, 7))):
            source.write(f"SKU-1,{month},5\n")

    row, = plan(export).recommendations
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
    assert all("Поставщик не указан" in row.sku_flags for row in unknown)
    # Selling SKUs without a known supplier go to the buyer with an order sized on a typical supplier.
    typical = [row for row in unknown if any(flag.startswith(TYPICAL_TIMING_FLAG) for flag in row.sku_flags)]
    assert typical and all(row.segment == Segment.CONDITIONAL and row.sales_qty_12m > 0 for row in typical)
    assert all(row.order_qty == 0 for row in unknown if row not in typical)
    # Without a supplier nobody can place the order, so it never counts as a supplier to order now.
    assert not any(summary.time_to_order for summary in facts.suppliers if not summary.supplier_id)
    # Seasonal SKUs are checked only against their season months.
    seasonal_checked = [row for row in rows.values() if row.seasonal_category and row.checked_stock_months]
    assert seasonal_checked and all(row.checked_stock_months <= 6 for row in seasonal_checked)
    missing_lead = {summary.supplier_id for summary in facts.suppliers if summary.timing.lead_days is None}
    assert missing_lead
    assert all(
        row.order_qty == 0 and "Не определено плечо поставки" in row.sku_flags
        for row in rows.values()
        if row.supplier_id in missing_lead and not any(flag.startswith(TYPICAL_TIMING_FLAG) for flag in row.sku_flags)
    )

    path = tmp_path / "order.xlsx"
    write_report(facts, path)
    workbook = load_workbook(path, read_only=True)
    try:
        orders = [row for row in workbook["Заказ по поставщикам"].values if row[1] in catalog]
        assert sorted(row[1] for row in orders) == sorted(row.sku_id for row in rows.values() if row.order_qty > 0)
        for group in (Action.DECIDE, Action.SKIP):
            header = next(row for row in workbook[group].values if row[3] == "SKU")
            data = [row for row in workbook[group].values if row[3] in catalog]
            assert sorted(row[3] for row in data) == sorted(
                row.sku_id for row in rows.values() if action(row) == group
            )
            assert all(row[0] and row[1] for row in data)
            if group == Action.SKIP:
                assert all(row[header.index("К заказу")] == 0 for row in data)
    finally:
        workbook.close()

def test_same_export_produces_identical_facts(export):
    assert plan(export) == plan(export)


def test_first_stockout_before_in_transit_receipt(export):
    (export / "export_meta.json").write_text('{"cutoff_date":"2026-01-31"}', encoding="utf-8")
    (export / "stock_monthly.csv").write_text(
        "sku_id,month,stock_qty\nSKU-1,2026-01,60\n", encoding="utf-8",
    )
    (export / "sales_monthly.csv").write_text(
        "sku_id,category,month,qty_sold,qty_returned,orders_cnt,markup_pct,partial_month\n"
        + "".join(f"SKU-1,tools,2025-{month:02},30,0,1,30,0\n" for month in range(1, 13))
        + "SKU-1,tools,2026-01,0,0,0,,1\n",
        encoding="utf-8",
    )
    orders = export / "purchase_orders.csv"
    orders.write_text(
        orders.read_text(encoding="utf-8").splitlines()[0] + "\n"
        "PO-1,SKU-1,SUP-1,import,2025-06-01,,100,completed,10,10,10,0\n"
        "PO-2,SKU-1,SUP-1,import,2025-09-01,2026-05-01,100,open,100,100,0,100\n",
        encoding="utf-8",
    )

    facts = plan(export)
    row = facts.recommendations[0]
    assert row.stock_until == date(2026, 3, 31)
    assert row.coverage_months == pytest.approx(2)
    assert row.stockout_risk
    assert row.stockout_days == 30
    assert row.lost_sales == pytest.approx(30)

    path = export / "order.xlsx"
    write_report(facts, path)
    with_workbook = load_workbook(path, read_only=True)
    try:
        _, risk = list(with_workbook["Риски дефицита"].values)
        assert risk[:7] == ("SKU-1", None, "SUP-1", 60, 100, "100 шт. — придёт 01.05.2026, через 90 дней", "2026-03-31")
        headers, forecast = list(with_workbook["Прогноз"].values)
        assert forecast[headers.index("Покрытие, мес")] == 2
    finally:
        with_workbook.close()


def test_supplier_order_date_can_be_after_the_next_cycle(export):
    (export / "export_meta.json").write_text('{"cutoff_date":"2026-01-31"}', encoding="utf-8")
    (export / "stock_monthly.csv").write_text(
        "sku_id,month,stock_qty\nSKU-1,2026-01,300\n", encoding="utf-8",
    )
    (export / "sales_monthly.csv").write_text(
        "sku_id,category,month,qty_sold,qty_returned,orders_cnt,markup_pct,partial_month\n"
        + "".join(f"SKU-1,tools,2025-{month:02},30,0,1,30,0\n" for month in range(1, 13))
        + "SKU-1,tools,2026-01,0,0,0,,1\n",
        encoding="utf-8",
    )
    orders = export / "purchase_orders.csv"
    orders.write_text(
        orders.read_text(encoding="utf-8").splitlines()[0] + "\n"
        "PO-1,SKU-1,SUP-1,import,2025-06-01,,100,completed,10,10,10,0\n"
        "PO-2,SKU-1,SUP-1,import,2025-07-31,,100,completed,10,10,10,0\n",
        encoding="utf-8",
    )
    facts = plan(export)
    row, = facts.recommendations
    supplier, = facts.suppliers
    assert row.coverage_months == pytest.approx(10)
    assert row.stock_until == date(2026, 11, 30)
    assert supplier.next_order_date == row.order_by == date(2026, 8, 2)
    assert row.order_qty > 0
    assert not supplier.time_to_order


def _seasonal_export(export, cutoff):
    (export / "export_meta.json").write_text(json.dumps({"cutoff_date": cutoff}), encoding="utf-8")
    sales = export / "sales_monthly.csv"
    sales.write_text(
        sales.read_text(encoding="utf-8").splitlines()[0] + "\n"
        "SKU-1,tools,2025-01,10,0,1,30,0\n"
        "SKU-1,tools,2025-02,10,0,1,30,0\n"
        "SKU-1,tools,2025-10,60,0,1,30,0\n"
        "SKU-1,tools,2025-11,40,0,1,30,0\n"
        "SKU-1,tools,2025-12,20,0,1,30,0\n"
        "SKU-1,tools,2026-02,10,0,1,30,0\n",
        encoding="utf-8",
    )
    (export / "stock_monthly.csv").write_text(
        "sku_id,month,stock_qty\nSKU-1,2026-01,0\n", encoding="utf-8",
    )
    orders = export / "purchase_orders.csv"
    orders.write_text(
        orders.read_text(encoding="utf-8").splitlines()[0] + "\n"
        "PO-1,SKU-1,SUP-1,import,2025-06-01,,100,completed,10,10,10,0\n"
        "PO-2,SKU-1,SUP-1,import,2025-07-31,,100,completed,10,10,10,0\n",
        encoding="utf-8",
    )
    (export / "receipts.csv").write_text(
        "sku_id,receipt_date,qty,supplier_id,supply_type\nSKU-1,2025-12-01,10,SUP-1,import\n",
        encoding="utf-8",
    )
    (export / "backtest.json").write_text(json.dumps({
        "cutoff": "2026-05-01",
        "origins": [],
        "metrics": [],
        "quantiles": {
            group: {"windows": 1, "p75": 1.25, "p85": 1.5, "p95": 1.5}
            for group in ("Регулярный", "Прерывистый")
        },
    }), encoding="utf-8")


@pytest.mark.parametrize(("cutoff", "status", "quantity", "actionable"), [
    ("2026-06-03", "Сезонный", 150, True),
    ("2026-07-01", "Поздний", (60 + 60 * 2 / 31) * 1.25, True),
    ("2026-08-15", "Не рекомендуется", (20 * 18 / 31) * 1.25, False),
    ("2026-09-15", "Приход после окна", 0, False),
])
def test_seasonal_order_and_late_order_decisions(export, cutoff, status, quantity, actionable):
    _seasonal_export(export, cutoff)
    facts = plan(export)
    row, = facts.recommendations
    season = row.seasonal_order
    assert season is not None
    assert season.window_start == date(2026, 10, 1)
    assert season.window_end == date(2026, 12, 31)
    assert season.deadline == date(2026, 6, 3)
    assert season.next_deadline == date(2027, 6, 3)
    assert season.status == status
    assert season.order_qty == pytest.approx(quantity)
    assert season.ordinary_service_qty == pytest.approx(quantity / 1.25 * 1.5)
    assert season.actionable == actionable
    if actionable:
        assert row.order_by == season.deadline
        assert row.order_qty == pytest.approx(quantity)
    if status == "Поздний":
        assert row.segment == Segment.CONDITIONAL
        assert "Рассмотреть ускоренную доставку" in row.sku_flags


def test_regular_sku_joins_the_suppliers_seasonal_order(export):
    _seasonal_export(export, "2026-05-15")
    skus = export / "sku_dim.csv"
    with skus.open("a", encoding="utf-8") as source:
        source.write(
            "SKU-2,steady,BRD-1,SUP-1,import,pcs,,0,0,1,2025-01,2025-12,12,360,30,0,2025-01\n"
        )
    with (export / "sales_monthly.csv").open("a", encoding="utf-8") as source:
        for month in range(1, 13):
            source.write(f"SKU-2,steady,2025-{month:02},30,0,1,30,0\n")
    seasonal, steady = plan(export).recommendations
    assert seasonal.seasonal_order is not None and seasonal.seasonal_order.actionable
    assert steady.regular_order_date < date(2026, 5, 15)
    assert steady.grouped_season_deadline == steady.order_by == date(2026, 6, 3)
    assert steady.order_qty > 0

    # This receipt arrives after the original arrival but before the grouped one.
    with (export / "purchase_orders.csv").open("a", encoding="utf-8") as source:
        source.write("PO-3,SKU-2,SUP-1,import,2025-07-31,2026-09-20,100,open,1000,1000,0,1000\n")
    _, steady = plan(export).recommendations
    assert steady.grouped_season_deadline == date(2026, 6, 3)
    assert steady.order_qty == 0


def test_seasonal_order_uses_known_lead_without_a_regular_cycle(export):
    _seasonal_export(export, "2026-06-03")
    orders = export / "purchase_orders.csv"
    lines = orders.read_text(encoding="utf-8").splitlines()
    orders.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
    facts = plan(export)
    row, = facts.recommendations
    supplier, = facts.suppliers
    assert supplier.timing.cycle_days is None
    assert supplier.time_to_order
    assert row.order_qty == pytest.approx(150)
    assert row.order_by == date(2026, 6, 3)


def test_between_seasons_only_regular_demand_is_replenished(export):
    _seasonal_export(export, "2026-01-01")
    orders = export / "purchase_orders.csv"
    orders.write_text(orders.read_text(encoding="utf-8").replace(",100,completed", ",0,completed"), encoding="utf-8")

    row, = plan(export).recommendations
    season = row.seasonal_order
    assert season is not None
    assert row.regular_order_qty == pytest.approx(10 + 100 / 31)
    assert season.stock_at_arrival == pytest.approx(20 - row.regular_order_qty)
    assert season.order_qty == pytest.approx(120 - season.stock_at_arrival)


def test_late_seasonal_order_below_one_multiple_is_not_recommended(export):
    _seasonal_export(export, "2026-07-01")
    orders = export / "purchase_orders.csv"
    orders.write_text(orders.read_text(encoding="utf-8").replace(",10,10,10,0", ",100,100,100,0"), encoding="utf-8")

    row, = plan(export).recommendations
    season = row.seasonal_order
    assert season is not None
    assert 75 < season.order_qty < 100
    assert season.status == "Не рекомендуется"
    assert row.order_qty == 0


def test_ordinary_order_covers_demand_just_after_a_season(export):
    _seasonal_export(export, "2026-07-01")
    orders = export / "purchase_orders.csv"
    orders.write_text(
        orders.read_text(encoding="utf-8").replace("2025-07-31", "2025-09-29"), encoding="utf-8",
    )
    row, = plan(export).recommendations
    season = row.seasonal_order
    assert season is not None and season.actionable
    assert row.regular_order_date == date(2026, 9, 2)
    assert row.regular_order_qty == pytest.approx(30 - season.stock_at_season_end)


def test_open_seasonal_batch_keeps_its_full_coverage_after_deadline(export):
    _seasonal_export(export, "2026-06-03")
    before, = plan(export).recommendations
    assert before.order_qty == pytest.approx(150)

    (export / "export_meta.json").write_text('{"cutoff_date":"2026-07-01"}', encoding="utf-8")
    with (export / "purchase_orders.csv").open("a", encoding="utf-8") as source:
        source.write("PO-3,SKU-1,SUP-1,import,2026-06-03,2026-10-01,100,open,150,150,0,150\n")
    after, = plan(export).recommendations
    assert after.in_transit == 150
    assert after.excess_threshold == pytest.approx(160)
    assert not after.excess


def test_long_lead_recommended_batch_does_not_become_excess(export):
    (export / "export_meta.json").write_text('{"cutoff_date":"2026-03-01"}', encoding="utf-8")
    sales = export / "sales_monthly.csv"
    sales.write_text(
        sales.read_text(encoding="utf-8").splitlines()[0] + "\n"
        + "".join(
            f"SKU-1,tools,2025-{month:02},{monthrange(2025, month)[1]},0,1,30,0\n"
            for month in range(1, 13)
        ),
        encoding="utf-8",
    )
    orders = export / "purchase_orders.csv"
    orders.write_text(
        orders.read_text(encoding="utf-8").splitlines()[0] + "\n"
        "PO-1,SKU-1,SUP-1,import,2025-07-04,,180,completed,10,10,10,0\n"
        "PO-2,SKU-1,SUP-1,import,2025-11-01,,180,completed,10,10,10,0\n",
        encoding="utf-8",
    )
    (export / "backtest.json").write_text(json.dumps({
        "cutoff": "2026-03-01",
        "origins": [],
        "metrics": [],
        "quantiles": {"Регулярный": {"windows": 1, "p75": 1.5, "p85": 1.5, "p95": 2}},
    }), encoding="utf-8")

    before = plan(export)
    assert before.recommendations[0].order_qty == pytest.approx(240)
    assert before.suppliers[0].order_qty == 240
    with orders.open("a", encoding="utf-8") as source:
        source.write("PO-3,SKU-1,SUP-1,import,2026-03-01,2026-09-17,180,open,240,240,0,240\n")
    after, = plan(export).recommendations
    assert after.in_transit == 240
    assert after.excess_threshold == pytest.approx(650)
    assert not after.excess


def test_run_history_compares_the_same_export_and_preserves_parameters(export, tmp_path, monkeypatch):
    history = tmp_path / "runs"
    monkeypatch.delenv("LLM_MAX_SKUS", raising=False)
    first = run_plan(export, history)
    assert json.loads((first / "parameters.json").read_text(encoding="utf-8"))["LLM_MAX_SKUS"] == 300
    monkeypatch.setenv("LLM_MAX_SKUS", "1")
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
            assert values["LLM_MAX_SKUS"] == parameters["LLM_MAX_SKUS"] == 1
            assert values["К закупке при наценке от, %"] == 25
        finally:
            workbook.close()

def test_new_decision_facts_compare_with_old_runs_without_rewriting_history(export, tmp_path):
    history = tmp_path / "runs"
    first = run_plan(export, history, stub_llm=True)
    old = json.loads((first / "facts.json").read_text(encoding="utf-8"))
    for row in old["recommendations"]:
        row.pop("decision")
    (first / "facts.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    original = {path.name: path.read_bytes() for path in first.iterdir() if path.is_file()}
    second = run_plan(export, history, stub_llm=True)
    assert json.loads((second / "diff.json").read_text(encoding="utf-8"))["changes"] == []
    assert {path.name: path.read_bytes() for path in first.iterdir() if path.is_file()} == original
    current = json.loads((second / "facts.json").read_text(encoding="utf-8"))
    assert all(row["decision"] is not None for row in current["recommendations"])


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
    # 27% is above the 25% threshold: the SKU returns to purchase at once, without a waiting band.
    assert facts["recommendations"][0]["segment"] == Segment.PURCHASE

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

    monkeypatch.setattr(segments, "PURCHASE_MARKUP_PCT", 31)
    second = run_plan(export, history)
    change, = json.loads((second / "diff.json").read_text(encoding="utf-8"))["changes"]
    assert change["segment"] == [Segment.PURCHASE, Segment.REVIEW]
    assert change["stockout_risk"] == [True, False]
    assert change["drivers"] == ["parameter_changed"]

    monkeypatch.setattr(segments, "PURCHASE_MARKUP_PCT", 25)
    third = run_plan(export, history)
    change, = json.loads((third / "diff.json").read_text(encoding="utf-8"))["changes"]
    assert change["segment"] == [Segment.REVIEW, Segment.PURCHASE]
    assert change["stockout_risk"] == [False, True]
    assert change["drivers"] == ["parameter_changed"]


def test_change_count_follows_action_groups():
    from purchase_planning.comparison import count_changes

    def change(old, new):
        return {"sku_id": "SKU-1", "order_qty": (0.0, 0.0), "segment": (old, new), "stockout_risk": (False, False),
                "drivers": [], "flags": []}

    counts = count_changes([
        change("Мёртвые", "Спящие"),  # both «Не заказывать»
        change("К закупке", "Условно к закупке"),
        change(None, "К закупке"),
    ])
    assert counts.segments == 2


def _run_cli(monkeypatch, *args: object) -> int:
    monkeypatch.setattr(sys, "argv", ["runs", *map(str, args)])
    return runs_main()


def test_rejected_export_republishes_previous_plan_with_reason(export, tmp_path, monkeypatch):
    history = tmp_path / "history"
    first = run_plan(export, history, stub_llm=True)
    assert json.loads((history / STATUS_FILENAME).read_text(encoding="utf-8"))["accepted"] is True
    (export / "stock_monthly.csv").write_text("", encoding="utf-8")

    site = tmp_path / "site"
    assert _run_cli(monkeypatch, export, "--history-dir", history, "--site-dir", site, "--stub-llm") == REJECTED_EXIT

    status = json.loads((site / STATUS_FILENAME).read_text(encoding="utf-8"))
    assert status["accepted"] is False
    assert status["reasons"] == ["stock_monthly.csv: файл пуст."]
    assert (status["run"], status["cutoff"]) == (first.name, "2026-08-13")
    assert (site / "index.html").read_bytes() == (first / "index.html").read_bytes()
    assert [path.name for path in history.iterdir() if path.is_dir()] == [first.name]


def test_rejected_first_export_publishes_nothing(export, tmp_path, monkeypatch):
    (export / "stock_monthly.csv").write_text("", encoding="utf-8")
    site = tmp_path / "site"
    assert _run_cli(monkeypatch, export, "--history-dir", tmp_path / "history", "--site-dir", site) == 1
    assert not site.exists()


def test_only_the_published_export_is_a_duplicate(export, tmp_path):
    history = tmp_path / "history"
    original = {path.name: path.read_bytes() for path in export.iterdir() if path.is_file()}
    run_plan(export, history, stub_llm=True, reject_duplicate=True)
    with pytest.raises(ExportValidationError, match="уже на портале"):
        run_plan(export, history, stub_llm=True, reject_duplicate=True)

    with (export / "receipts.csv").open("a", encoding="utf-8") as source:
        source.write("SKU-1,2026-08-10,10,SUP-1,import\n")
    run_plan(export, history, stub_llm=True, reject_duplicate=True)
    for name, content in original.items():
        (export / name).write_bytes(content)
    rollback = run_plan(export, history, stub_llm=True, reject_duplicate=True)
    assert rollback.name == "000003"
