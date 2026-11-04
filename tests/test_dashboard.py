from datetime import date, timedelta
from xml.etree import ElementTree

from openpyxl import load_workbook

from purchase_planning.dashboard import write_dashboard
from purchase_planning.explanations import Explanations
from purchase_planning.export import REQUIRED_COLUMNS
from purchase_planning.facts import (
    ESTIMATED_CUTOFF_SOURCE, UNCONFIRMED_ORDERS_LABEL, BacktestMetric, BacktestResult, OrderFlag, PlanFacts,
    Recommendation, SeasonalOrder, SeasonStatus, Segment, SupplierSummary, SupplierTiming,
)
from purchase_planning.report import write_report
from purchase_planning.sku_labels import load_sku_labels


def test_dashboard_matches_workbook_and_displays_facts(tmp_path):
    cutoff = date(2026, 8, 13)
    run_date = date(2026, 9, 18)
    timing = SupplierTiming(140, "История", 90)
    facts = PlanFacts(
        cutoff, cutoff,
        (
            Recommendation("SKU-1", "А<&", 2.1, order_flags=("Хвост заказа",)),
            Recommendation("SKU-2", "А<&", 5.1, segment=Segment.CONDITIONAL, markup_pct_12m=22,
                           order_flags=(OrderFlag.UNCONFIRMED,)),
            Recommendation("SKU-3", "Б", 1, order_flags=("Зависший заказ",)),
        ),
        cutoff_source=ESTIMATED_CUTOFF_SOURCE,
        suppliers=(SupplierSummary("Б", timing, 1, False, date(2026, 10, 1)),
                   SupplierSummary("А<&", timing, 7.2, True)),
        unconfirmed_order_lines=2,
    )
    report = tmp_path / "report.xlsx"
    dashboard = tmp_path / "index.html"
    write_report(facts, report, run_date=run_date)
    write_dashboard(facts, dashboard, run_date=run_date, previous_run=None, changes=[])

    page = ElementTree.fromstring(dashboard.read_text(encoding="utf-8").split("\n", 1)[1])
    suppliers = page.findall(".//section[@id='orders']/div/div/details/summary")
    workbook = load_workbook(report)
    try:
        totals = [(row[0], row[3]) for row in workbook.active.values if row[1] == "Итого"]
        assert [(row[0].text, int(row.find("span[@class='batch']").text.split()[-2])) for row in suppliers] == totals
        assert totals == [("А<&", 9), ("Б", 1)]
        cutoff_row, _, _, headers, *rows = workbook.active.values
        assert cutoff_row[:2] == ("Дата среза", f"≈ 13.08.2026 · {ESTIMATED_CUTOFF_SOURCE}")
        assert dict(workbook["Сводка"].values)[UNCONFIRMED_ORDERS_LABEL] == 2
        confirmation = headers.index("Подтверждение байера")
        assert {row[1]: row[confirmation] for row in rows if row[1] != "Итого"} == {
            "SKU-1": None, "SKU-2": "Требует подтверждения", "SKU-3": None,
        }
        order_flags = headers.index("Флаги заказов")
        assert next(row[order_flags] for row in rows if row[1] == "SKU-2") == OrderFlag.UNCONFIRMED
    finally:
        workbook.close()
    assert [row[1].text for row in suppliers] == [
        "Когда заказать — Сейчас · запас ниже точки заказа", "Когда заказать — 01.10.2026",
    ]
    assert len(page.findall(".//section[@id='orders']//ul/li")) == 3
    assert page.findall(".//details[@class='sku-detail']/dl/dt")[0].text == "К заказу, шт."
    assert page.find(".//header//a[@class='upload-action primary']").get("href") == "#upload"
    assert page.find(".//header//a[@href='report.xlsx']").text == "Скачать черновик XLSX"
    confirmation = page.find(".//section[@class='confirmation']")
    assert confirmation.find("h3").text == "Требуют подтверждения"
    assert confirmation.find(".//small[@class='confirmation-reason']").text == "Наценка ниже 25%"
    assert "6 шт. к заказу" in "".join(confirmation.itertext())
    metrics = {metric.find("dt").text: metric.find("dd").text for metric in page.findall(".//dl[@class='decision']/div")}
    assert metrics["Рекомендация на дату среза, шт."] == "3"
    assert metrics["Требуют подтверждения, шт."] == "6"
    assert metrics[UNCONFIRMED_ORDERS_LABEL] == "2"
    visible_text = "".join(page.itertext())
    assert page.find(".//a[@href='#guide']") is not None
    assert page.find(".//section[@id='guide']/h2").text == "Как работать с порталом"
    assert page.find(".//header//a[@href='#upload']").text == "Обновить выгрузку ERP"
    upload = page.find(".//section[@id='upload']")
    assert page.find(".//section[@id='guide']//a[@href='#upload']") is not None
    assert any(link.get("href", "").endswith("/tree/main/inbox") for link in upload.findall(".//a"))
    guide_files = {node.text for node in upload.findall(".//code")}
    assert {name for name in guide_files if name.endswith(".csv")} == {f"{name}.csv" for name in REQUIRED_COLUMNS}
    assert "export_meta.json" not in "".join(upload.itertext())
    assert "Устаревшая выгрузка — черновик, не размещать" in "".join(page.find(".//header").itertext())
    for value in (f"Срез ≈ 13.08.2026 · {ESTIMATED_CUTOFF_SOURCE}", "36 дней", "Первый запуск",
                  "Зависший заказ", "Хвост заказа", "Условно к закупке", OrderFlag.UNCONFIRMED):
        assert value in visible_text


def test_risk_table_uses_workbook_order_and_hides_losses_below_one(tmp_path):
    cutoff = date(2026, 8, 13)
    later = date(2026, 9, 1)
    facts = PlanFacts(cutoff, cutoff, (
        Recommendation("SKU-1", "SUP-1", 0, stockout_risk=True, stock_until=cutoff, lost_sales=1),
        Recommendation("SKU-2", "SUP-1", 1, stockout_risk=True, stock_until=cutoff, lost_sales=2.6),
        Recommendation("SKU-3", "SUP-1", 0, stockout_risk=True, stock_until=cutoff, lost_sales=0.99),
        Recommendation("SKU-4", "SUP-1", 0, stockout_risk=True, stock_until=later, lost_sales=10),
        Recommendation("SKU-5", "SUP-2", 0, excess=True, coverage_months=12.34, price_decile=8,
                       order_flags=(OrderFlag.RESCHEDULE,)),
    ))
    report = tmp_path / "report.xlsx"
    dashboard = tmp_path / "index.html"
    write_report(facts, report, run_date=cutoff)
    write_dashboard(
        facts, dashboard, run_date=cutoff, previous_run="000001", changes=[],
        explanations=Explanations("Сводка <&", {"SKU-2": "К заказу 1 шт."}),
    )

    page = ElementTree.fromstring(dashboard.read_text(encoding="utf-8").split("\n", 1)[1])
    workbook = load_workbook(report)
    try:
        expected = [("SKU-2", 3), ("SKU-1", 1), ("SKU-3", 1), ("SKU-4", 10)]
        assert [(row[0], row[-1]) for row in list(workbook["Риски дефицита"].values)[1:]] == expected
        rows = page.findall(".//div[@aria-label='Риски дефицита']/table/tbody/tr")
        assert [(row[0].text, int(row[1].text)) for row in rows] == [*expected[:2], expected[-1]]
        assert workbook["Избыток"]["G2"].value == "> 12,3"
        assert page.find(".//div[@aria-label='Избыток']/table/tbody/tr/td[5]").text == "> 12,3"
    finally:
        workbook.close()
    visible_text = "".join(page.itertext())
    assert "В XLSX есть ещё 1 SKU с оценкой потерь менее 1 шт." in visible_text
    assert "Пояснение LLM · Сводка <&" in visible_text
    assert "Пояснение LLM · К заказу 1 шт." in visible_text


def test_dashboard_splits_supplier_batches_and_uses_calendar_dates_in_decision(tmp_path):
    day = date(2026, 8, 13)
    in_30 = day + timedelta(days=30)
    in_31 = day + timedelta(days=31)
    timing = SupplierTiming(20, "История", 60)

    def season(deadline):
        return SeasonalOrder(
            window_start=deadline + timedelta(days=20), window_end=deadline + timedelta(days=60),
            deadline=deadline, days_late=0, months_remaining=1.3, order_qty=5.1, ordinary_service_qty=6,
            stock_at_season_end=0, next_deadline=date(2027, 9, 12), status=SeasonStatus.ON_TIME,
            stock_at_arrival=0, target_stock=6, safety_stock=0,
        )

    facts = PlanFacts(day, day, (
        Recommendation("SKU-1", "SUP-Z", 7.2, regular_order_qty=2.1, regular_order_date=day - timedelta(days=1),
                       seasonal_order=season(in_30), stock_at_calculation=0),
        Recommendation("SKU-2", "SUP-A", 5.1, regular_order_qty=0, seasonal_order=season(in_31)),
        Recommendation("SKU-3", "SUP-B", 4, regular_order_qty=4, regular_order_date=in_30, segment=Segment.CONDITIONAL),
        Recommendation("SKU-4", "SUP-Z", 2, regular_order_qty=2, regular_order_date=in_30),
    ), suppliers=(
        SupplierSummary("SUP-A", timing, 6, False, in_31),
        SupplierSummary("SUP-B", timing, 4, True, in_30),
        SupplierSummary("SUP-Z", timing, 9, True, day - timedelta(days=1)),
    ))
    dashboard = tmp_path / "index.html"
    write_dashboard(facts, dashboard, run_date=day + timedelta(days=2), previous_run=None, changes=[])
    page = ElementTree.fromstring(dashboard.read_text(encoding="utf-8").split("\n", 1)[1])
    suppliers = page.findall(".//section[@id='orders']/div/div/details/summary")
    assert [supplier.find("strong").text for supplier in suppliers] == ["SUP-Z", "SUP-B", "SUP-A"]
    assert [batch.text for batch in suppliers[0].findall("span[@class='batch']")] == [
        "Обычная партия — Сейчас — 3 шт.",
        "Обычная партия — 12.09.2026 — 2 шт.",
        "Сезонная партия — до 12.09.2026 — 6 шт.",
    ]
    assert "запас ниже точки заказа" in "".join(suppliers[0].itertext())
    assert "опоздание" not in "".join(page.itertext())
    assert "2 дня на дату запуска" in "".join(page.itertext())
    metrics = {metric.find("dt").text: metric.find("dd").text for metric in page.findall(".//dl[@class='decision']/div")}
    assert metrics["К заказу сейчас, шт."] == "3"
    assert metrics["Требуют подтверждения, шт."] == "0"
    assert metrics["Сезонные партии с крайним сроком в ближайшие 90 дней, шт."] == "12"
    assert metrics["Поставщиков в следующие 30 дней"] == "2"
    primary_action = page.find(".//header//a[@class='primary']")
    assert primary_action.get("href") == "#orders"
    assert primary_action.text == "Проверить заказ по поставщикам"
    assert page.find(".//header//a[@href='report.xlsx']").text == "Скачать XLSX"
    assert {link.get("href"): link.text for link in page.findall(".//section[@id='overview']//dd/a")} == {
        "#orders": "1", "#risks": "0",
    }
    calendar = page.findall(".//{http://www.w3.org/2000/svg}text")
    assert [node.text for node in calendar[:4]] == ["авг. 2026", "1", "сент. 2026", "3"]


def test_sku_labels_on_page_and_in_workbook(tmp_path):
    (tmp_path / "sku_dim.csv").write_text(
        "sku_id,category,brand_id,price_decile\n"
        "SKU-1,space_heaters,BRD-A<&,7\n"
        "SKU-2,new_category,,4\n"
        "SKU-3,,,\n",
        encoding="utf-8",
    )
    day = date(2026, 8, 13)
    season = SeasonalOrder(
        window_start=day, window_end=day, deadline=day, days_late=0, months_remaining=1,
        order_qty=1, ordinary_service_qty=1, stock_at_season_end=0, next_deadline=day,
        status=SeasonStatus.ON_TIME, stock_at_arrival=0, target_stock=1, safety_stock=0,
    )
    facts = PlanFacts(day, day, tuple(
        Recommendation(sku, "SUP-1", 1, category=category, seasonal_order=season, excess=True,
                       stockout_risk=True, stock_until=day, lost_sales=2)
        for sku, category in (("SKU-1", "space_heaters"), ("SKU-2", "new_category"), ("SKU-3", ""))
    ))
    labels = load_sku_labels(tmp_path)
    report, dashboard = tmp_path / "report.xlsx", tmp_path / "index.html"
    write_report(facts, report, run_date=day, sku_labels=labels)
    write_dashboard(facts, dashboard, run_date=day, previous_run=None, changes=[], sku_labels=labels)

    expected = {"SKU-1": "Обогреватели · BRD-A<& · цена 7/10", "SKU-2": "new_category · цена 4/10"}
    page = ElementTree.fromstring(dashboard.read_text(encoding="utf-8").split("\n", 1)[1])
    for section, sku_path in (
        ("orders", ".//ul/li/details/summary/span/strong"),
        ("risks", ".//tbody/tr/td[1]"),
        ("excess", ".//tbody/tr/td[1]"),
        ("seasons", ".//ul/li"),
    ):
        section_node = page.find(f".//section[@id='{section}']")
        assert {node.text for node in section_node.findall(sku_path)} == {*expected, "SKU-3"}
        assert {node.text for node in section_node.findall(".//small[@class='sku-label']")} == set(expected.values())

    workbook = load_workbook(report)
    try:
        for sheet in workbook:
            if sheet.title in {"Сводка", "Бэктест", "Параметры"}:
                continue
            values = iter(sheet.values)
            header = next(row for row in values if "SKU" in row)
            sku_index = header.index("SKU")
            assert header[sku_index + 1] == "Товар"
            for row in values:
                assert row[sku_index + 1] == expected.get(row[sku_index])
                if sheet.title == "Сезонные заказы":
                    assert row[header.index("Категория")] == {
                        "SKU-1": "Обогреватели", "SKU-2": "new_category", "SKU-3": None,
                    }[row[sku_index]]
    finally:
        workbook.close()


def test_analytics_terms_have_descriptions(tmp_path):
    day = date(2026, 8, 13)
    backtest = BacktestResult(day, (day,), (BacktestMetric("Все", "Все", 100, .38, -.032, None, None),), {})
    facts = PlanFacts(day, day, (), backtest=backtest)
    dashboard = tmp_path / "index.html"
    write_dashboard(facts, dashboard, run_date=day, previous_run=None, changes=[])

    page = ElementTree.fromstring(dashboard.read_text(encoding="utf-8").split("\n", 1)[1])
    for section, count in (("forecast-quality", 2), ("stock", 3), ("segments", 5), ("order-flags", 4)):
        node = page.find(f".//section[@id='{section}']")
        descriptions = {hint.attrib["id"]: hint.text for hint in node.findall(".//span[@role='tooltip']")}
        buttons = node.findall(".//button[@aria-describedby]")
        assert len(buttons) == len(descriptions) == count
        for button in buttons:
            assert descriptions[button.attrib["aria-describedby"]]
    assert page.find(".//span[@id='accuracy-1']/../button").text == "WAPE 38,0%"
    assert page.find(".//span[@id='accuracy-2']/../button").text == "Смещение -3,2%"
