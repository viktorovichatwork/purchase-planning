import csv
from collections import Counter
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree

import pytest
from openpyxl import load_workbook

from purchase_planning.actions import Action, SkipReason, action, skip_reason

from purchase_planning.dashboard import write_dashboard
from purchase_planning.explanations import Explanations
from purchase_planning.export import REQUIRED_COLUMNS
from purchase_planning.facts import (
    ESTIMATED_CUTOFF_SOURCE, UNCONFIRMED_ORDERS_LABEL, BacktestMetric, CategoryProfile, BacktestResult, OrderFlag, PlanFacts,
    Recommendation, SeasonalOrder, SeasonStatus, Segment, SupplierSummary, SupplierTiming,
)
from purchase_planning.planner import plan
from purchase_planning.report import write_report
from purchase_planning.summary import plan_summary
from purchase_planning.sku_labels import load_sku_labels


def test_dashboard_matches_workbook_and_displays_facts(tmp_path):
    cutoff = date(2026, 8, 13)
    run_date = date(2026, 9, 18)
    timing = SupplierTiming(140, "История", 90)
    facts = PlanFacts(
        cutoff, cutoff,
        (
            Recommendation("SKU-1", "А<&", 2.1, order_flags=("Хвост заказа",)),
            Recommendation("SKU-2", "А<&", 5.1, segment=Segment.CONDITIONAL, low_data=True,
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
        assert cutoff_row[:2] == ("Данные ERP на", "13.08.2026")
        summary_sheet = dict(workbook["Сводка"].values)
        assert summary_sheet[UNCONFIRMED_ORDERS_LABEL] == 2
        confirmation = headers.index("Решить закупщику: почему")
        assert {row[1]: row[confirmation] for row in rows if row[1] != "Итого"} == {
            "SKU-1": None, "SKU-2": "Мало истории продаж — прогноз по похожим товарам", "SKU-3": None,
        }
        _, decide = workbook["Решить закупщику"].values
        assert decide[:4] == (
            "Мало истории продаж — прогноз по похожим товарам", "Проверить количество", "А<&", "SKU-2",
        )
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
    assert page.find(".//header//a[@href='report.xlsx']").text == "Скачать XLSX"
    confirmation = page.find(".//section[@class='confirmation']")
    assert confirmation.find("h3").text == "Решить закупщику"
    assert confirmation.find(".//small[@class='confirmation-reason']").text == (
        "Мало истории продаж — прогноз по похожим товарам → Проверить количество"
    )
    assert "6 шт. к заказу" in "".join(confirmation.itertext())
    metrics = {metric.find("dt").text: "".join(metric.find("dd").itertext()) for metric in page.findall(".//dl[@class='decision']/div")}
    assert metrics["Пора заказывать по данным на 13.08.2026, SKU"] == "1"
    assert metrics["Решить закупщику, SKU"] == "1"
    assert UNCONFIRMED_ORDERS_LABEL not in metrics
    # Every tile is a count of SKU, links to its tab and matches the XLSX «Сводка» sheet.
    assert all(label.endswith(", SKU") for label in metrics)
    tabs = {section.get("id") for section in page.findall(".//section[@data-view]")}
    assert {link.get("href")[1:] for link in page.findall(".//section[@id='overview']//dd/a")} <= tabs
    assert {label.replace(" по данным на 13.08.2026", ""): int(value) for label, value in metrics.items()}.items() <= (
        summary_sheet.items()
    )
    assert "В группе «Заказать» 2 SKU: пора заказывать — 1 SKU (3 шт. у 1 поставщика)" in "".join(
        page.find(".//section[@id='orders']").itertext()
    )
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
    header_text = "".join(page.find(".//header").itertext())
    assert "Устаревшие данные" in header_text and "черновик" not in visible_text.lower()
    assert "Данным 36 дней: остатки на складе уже другие." in header_text
    for value in ("Остатки и продажи из ERP на 13.08.2026 · пересчитано 18.09.2026", "36 дней", "Первый запуск",
                  "Зависший заказ", "Хвост заказа", "Решить закупщику", OrderFlag.UNCONFIRMED):
        assert value in visible_text


def test_risk_table_lists_the_same_skus_as_the_workbook(tmp_path):
    cutoff = date(2026, 8, 13)
    later = date(2026, 9, 1)
    facts = PlanFacts(cutoff, cutoff, (
        Recommendation("SKU-1", "SUP-1", 0, stockout_risk=True, stock_until=cutoff, lost_sales=1),
        Recommendation("SKU-2", "SUP-1", 1, stockout_risk=True, stock_until=cutoff, lost_sales=2.6),
        Recommendation("SKU-3", "SUP-1", 0, stockout_risk=True, stock_until=cutoff, lost_sales=0.99),
        Recommendation("SKU-4", "SUP-1", 0, stockout_risk=True, stock_until=later, lost_sales=10),
        Recommendation("SKU-6", "SUP-1", 0, stockout_risk=True, stock_until=cutoff, lost_sales=0.3),
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
        expected = [("SKU-2", 3), ("SKU-1", 1), ("SKU-3", 1), ("SKU-6", 0), ("SKU-4", 10)]
        assert [(row[0], row[-1]) for row in list(workbook["Риски дефицита"].values)[1:]] == expected
        rows = page.findall(".//div[@aria-label='Риски дефицита']/table/tbody/tr")
        assert [(row[0].text, row[1].text) for row in rows] == [
            ("SKU-2", "3"), ("SKU-1", "1"), ("SKU-3", "1"), ("SKU-6", "меньше 1"), ("SKU-4", "10"),
        ]
        assert workbook["Избыток"]["H2"].value == "> 12,3"
        assert page.find(".//div[@aria-label='Избыток']/table/tbody/tr/td[6]").text == "> 12,3"
    finally:
        workbook.close()
    visible_text = "".join(page.itertext())
    assert "В XLSX есть ещё" not in visible_text
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
    report = tmp_path / "report.xlsx"
    write_report(facts, report, run_date=day)
    workbook = load_workbook(report)
    try:
        totals = [(row[0], row[3]) for row in workbook.active.values if row[1] == "Итого"]
        assert totals == [("SUP-Z", 11), ("SUP-B", 4), ("SUP-A", 6)]
        _, _, headers, *report_rows = workbook.active.values
        supplier_date = headers.index("Дата заказа у поставщика")
        # The ordinary batch is due even when the legacy SKU-level order_by is absent.
        assert report_rows[0][supplier_date] == "Сейчас · запас ниже точки заказа"
    finally:
        workbook.close()
    assert [batch.text for batch in suppliers[0].findall("span[@class='batch']")] == [
        "Обычная партия — Сейчас — 3 шт.",
        "Обычная партия — 12.09.2026 — 2 шт.",
        "Сезонная партия — до 12.09.2026 — 6 шт.",
    ]
    assert "запас ниже точки заказа" in "".join(suppliers[0].itertext())
    assert "опоздание" not in "".join(page.itertext())
    assert "Остатки и продажи из ERP на 13.08.2026 · пересчитано 15.08.2026" in "".join(page.itertext())
    metrics = {metric.find("dt").text: "".join(metric.find("dd").itertext()) for metric in page.findall(".//dl[@class='decision']/div")}
    assert metrics == {
        "Пора заказывать, SKU": "1", "Решить закупщику, SKU": "1", "Не заказывать, SKU": "0",
        "Риск дефицита, SKU": "0", "Заказать в ближайшие 30 дней, SKU": "1",
        "Сезонный заказ в ближайшие 90 дней, SKU": "2", "Избыток, SKU": "0",
    }
    primary_action = page.find(".//header//a[@class='primary']")
    assert primary_action.get("href") == "#orders"
    assert primary_action.text == "Проверить заказ по поставщикам"
    assert page.find(".//header//a[@href='report.xlsx']").text == "Скачать XLSX"
    assert [(link.get("href"), link.text) for link in page.findall(".//section[@id='overview']//dd/a")] == [
        ("#orders", "1"), ("#decide", "1"), ("#skip", "0"), ("#risks", "0"),
        ("#orders", "1"), ("#seasons", "2"), ("#excess", "0"),
    ]
    assert "у 2 SKU" in "".join(page.find(".//section[@id='seasons']").itertext())
    calendar = page.findall(".//{http://www.w3.org/2000/svg}text")
    assert [node.text for node in calendar[:4]] == ["авг. 2026", "1", "сент. 2026", "4"]


@pytest.mark.parametrize(("offset", "quantity", "status", "expected"), [
    (0, 1, SeasonStatus.ON_TIME, 1),
    (90, 1, SeasonStatus.ON_TIME, 1),
    (91, 1, SeasonStatus.ON_TIME, 0),
    (-1, 1, SeasonStatus.LATE, 0),
    (30, 0, SeasonStatus.ON_TIME, 0),
    (30, 1, SeasonStatus.NOT_RECOMMENDED, 0),
])
def test_season_tile_counts_only_needed_batches_within_the_deadline_window(offset, quantity, status, expected):
    day = date(2026, 8, 13)
    deadline = day + timedelta(days=offset)
    season = SeasonalOrder(
        window_start=deadline + timedelta(days=20), window_end=deadline + timedelta(days=60),
        deadline=deadline, days_late=max(0, -offset), months_remaining=2,
        order_qty=quantity, ordinary_service_qty=quantity, stock_at_season_end=0,
        next_deadline=day + timedelta(days=60), status=status,
        stock_at_arrival=0, target_stock=quantity, safety_stock=0,
    )
    # An ordinary order must not make a zero seasonal batch count; an overdue batch
    # must not be counted against next year's deadline using this year's quantity.
    row = Recommendation("SKU-1", "SUP-1", 10 + quantity, regular_order_qty=10, seasonal_order=season)
    facts = PlanFacts(day, day, (row,))
    assert plan_summary(facts).season_soon == expected
    assert plan_summary(replace(facts, recommendations=(replace(row, seasonal_order=None),))).season_soon == 0


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

    expected = {"SKU-1": "Обогреватели · BRD-A<& · ценовая группа 7 из 10", "SKU-2": "new_category · ценовая группа 4 из 10"}
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
    for section, count in (("forecast-quality", 2), ("stock", 3), ("segments", 3), ("order-flags", 4)):
        node = page.find(f".//section[@id='{section}']")
        descriptions = {hint.attrib["id"]: hint.text for hint in node.findall(".//span[@role='tooltip']")}
        buttons = node.findall(".//button[@aria-describedby]")
        assert len(buttons) == len(descriptions) == count
        for button in buttons:
            assert descriptions[button.attrib["aria-describedby"]]
    assert page.find(".//span[@id='accuracy-1']/../button").text == "WAPE 38,0%"
    assert page.find(".//span[@id='accuracy-2']/../button").text == "Смещение -3,2%"


def test_table_headers_explain_columns_and_seasons_explain_grouping(tmp_path):
    day = date(2026, 8, 13)
    heaters = CategoryProfile("space_heaters", (0, 0, 0, 0, 0, 0, 0, 0, .3, .4, .2, .1), (9, 10, 11, 12))
    facts = PlanFacts(day, day, (
        Recommendation("SKU-1", "SUP-1", 0, excess=True, excess_threshold=10, price_decile=7, stock_at_calculation=40),
    ), category_profiles=(heaters,))
    dashboard = tmp_path / "index.html"
    write_dashboard(facts, dashboard, run_date=day, previous_run=None, changes=[])
    page = ElementTree.fromstring(dashboard.read_text(encoding="utf-8").split("\n", 1)[1])
    headers = {th.text: th.get("title") for th in page.findall(".//section[@id='excess']//th")}
    assert headers["Порог избытка, шт."].startswith("Сколько товара системе достаточно иметь.")
    assert headers["Ценовая группа"] and headers["Открытый заказ"]
    legend = [dt.text for dt in page.findall(".//section[@id='excess']//details[@class='columns']//dt")]
    assert "Порог избытка, шт." in legend
    seasons = "".join(page.find(".//section[@id='seasons']").itertext())
    assert "Нет позиций." in seasons


def test_methodology_tab_renders_the_plain_language_methodology(tmp_path):
    from purchase_planning.methodology import markdown_to_html

    html = markdown_to_html(
        "# Заголовок\n\n## Шаг\n\nТекст **жирный**\nпродолжение.\n\n- пункт\n  дальше\n- второй\n\n"
        "| А | Б |\n|---|---|\n| 1 < 2 | 3 |\n\n> **К заказу** = прогноз\n> минус остаток\n"
    )
    assert "Заголовок" not in html
    assert "<h3>Шаг</h3>" in html
    assert "<p>Текст <strong>жирный</strong> продолжение.</p>" in html
    assert "<ul><li>пункт дальше</li><li>второй</li></ul>" in html
    assert "<td>1 &lt; 2</td>" in html
    assert "<blockquote><p><strong>К заказу</strong> = прогноз минус остаток</p></blockquote>" in html

    day = date(2026, 8, 13)
    dashboard = tmp_path / "index.html"
    write_dashboard(PlanFacts(day, day, ()), dashboard, run_date=day, previous_run=None, changes=[])
    page = ElementTree.fromstring(dashboard.read_text(encoding="utf-8").split("\n", 1)[1])
    assert page.find(".//nav[@class='workspace-nav']/a[@href='#method']").text == "Методика"
    method = page.find(".//section[@id='method']")
    assert method.get("data-view") == ""
    text = "".join(method.itertext())
    assert "Шаг 1. Сколько товара будут покупать" in text and "Три группы товаров" in text


def test_methodology_handles_indented_lists_and_quotes_in_links():
    from purchase_planning.methodology import markdown_to_html

    html = markdown_to_html('  - Первый\n    продолжение\n  - Второй\n\n  1. Шаг\n  2. Другой шаг\n')
    assert html == '<ul><li>Первый продолжение</li><li>Второй</li></ul>\n<ol><li>Шаг</li><li>Другой шаг</li></ol>'
    link = ElementTree.fromstring(markdown_to_html('[Ссылка](https://example.com/"test")'))[0]
    assert link.attrib == {"href": 'https://example.com/"test"'}


def test_pilot_export_tiles_tabs_workbook_and_task_numbers_agree(tmp_path):
    """End-to-end check on the real export: tiles match their groups in the tabs and XLSX sheets,
    and the counts the task (README «Задача») gives come out the same."""
    directory = Path(__file__).resolve().parents[1]
    facts = plan(directory)
    report = tmp_path / "report.xlsx"
    dashboard = tmp_path / "index.html"
    write_report(facts, report, run_date=facts.cutoff)
    write_dashboard(facts, dashboard, run_date=facts.cutoff, previous_run=None, changes=[])
    page = ElementTree.fromstring(dashboard.read_text(encoding="utf-8").split("\n", 1)[1])
    tiles = {
        metric.find("dd/a").get("href")[1:] + ":" + metric.find("dt").text: int(metric.find("dd/a").text.replace(" ", ""))
        for metric in page.findall(".//section[@id='overview']//dl[@class='decision']/div")
    }

    def table_rows(view: str) -> int:
        return len(page.findall(f".//section[@id='{view}']//table/tbody/tr"))

    summary = plan_summary(facts)
    assert tiles["decide:Решить закупщику, SKU"] == table_rows("decide") == summary.decide
    assert tiles["skip:Не заказывать, SKU"] == table_rows("skip") == summary.skip_total
    assert tiles["risks:Риск дефицита, SKU"] == table_rows("risks")
    assert tiles["excess:Избыток, SKU"] == table_rows("excess")
    assert tiles["orders:Пора заказывать, SKU"] == summary.order_now
    orders_text = "".join(page.find(".//section[@id='orders']").itertext())
    assert f"пора заказывать — {summary.order_now} SKU" in orders_text
    due = [node for node in page.findall(".//section[@id='orders']/div/div/details/summary/span")
           if node.text == "Когда заказать — Сейчас" or (node.text or "").startswith("Когда заказать — Сейчас ")]
    assert f"Срок заказа «Сейчас» — у {len(due)} поставщик" in orders_text
    assert len(due) == summary.due_suppliers
    seasons_text = "".join(page.find(".//section[@id='seasons']").itertext())
    assert f"у {tiles['seasons:Сезонный заказ в ближайшие 90 дней, SKU']} SKU" in seasons_text
    expected_season_skus = {
        row.sku_id for row in facts.recommendations
        if (season := row.seasonal_order) and season.order_qty > 0
        and season.status in {SeasonStatus.ON_TIME, SeasonStatus.LATE}
        and facts.as_of <= season.deadline <= facts.as_of + timedelta(days=90)
    }
    marked_season_skus = {
        node.text for node in page.findall(".//section[@id='seasons']//li[@data-season-soon='true']")
    }
    assert marked_season_skus == expected_season_skus
    assert len(marked_season_skus) == summary.season_soon == 70
    groups = Counter(action(row) for row in facts.recommendations)
    assert summary.order_total == groups[Action.ORDER]
    assert summary.order_total + summary.decide + summary.skip_total == len(facts.recommendations) == 791

    workbook = load_workbook(report, read_only=True)
    try:
        sheet = dict(workbook["Сводка"].values)
        assert {label.split(":", 1)[1]: count for label, count in tiles.items()}.items() <= sheet.items()
        for name, count in (("Решить закупщику", summary.decide), ("Не заказывать", summary.skip_total),
                            ("Риски дефицита", summary.risks), ("Избыток", summary.excess)):
            assert workbook[name].max_row - 1 == count, name
        seasonal_headers, *seasonal_rows = workbook["Сезонные заказы"].values
        marked_column = seasonal_headers.index("Сезонный заказ в ближайшие 90 дней")
        assert {row[0] for row in seasonal_rows if row[marked_column] == "Да"} == expected_season_skus

        skip_headers, *skip_rows = workbook["Не заказывать"].values
        workbook_skus = [row[skip_headers.index("SKU")] for row in skip_rows]
        portal_skus = [
            row.find("td").text for row in page.findall(".//section[@id='skip']//table/tbody/tr")
        ]
        assert workbook_skus == portal_skus

        order_values = list(workbook["Заказ по поставщикам"].values)
        header_index = next(i for i, row in enumerate(order_values) if "Пора заказывать у поставщика" in row)
        order_headers = order_values[header_index]
        due_column = order_headers.index("Пора заказывать у поставщика")
        date_column = order_headers.index("Дата заказа у поставщика")
        portal_due = {
            node.find("strong").text
            for node in page.findall(".//section[@id='orders']/div/div/details/summary")
            if "Когда заказать — Сейчас" in "".join(node.itertext())
        }
        totals = [row for row in order_values[header_index + 1:] if row[1] == "Итого"]
        assert {row[0] for row in totals if row[due_column] == "Да"} == portal_due
        later_supplier = next(row for row in totals if row[0] == "SUP-FBCEC0")
        assert (later_supplier[due_column], later_supplier[date_column]) == ("Нет", "2026-09-16")
    finally:
        workbook.close()

    # The task: 68 SKU with a markup below 25% out of the SKUs with a known markup, 378 SKU without one.
    with (directory / "sku_dim.csv").open(encoding="utf-8", newline="") as source:
        catalog = {row["sku_id"]: row["avg_markup_pct_12m"] for row in csv.DictReader(source)}
    rows = {row.sku_id: row for row in facts.recommendations}
    known = {sku: float(value) for sku, value in catalog.items() if value}
    assert all(rows[sku].markup_pct_12m == pytest.approx(value, abs=0.5) for sku, value in known.items())
    low = {sku for sku, value in known.items() if value < 25}
    assert len(low) == summary.skip[SkipReason.PRICE] == 68
    assert {skip_reason(rows[sku]) for sku in low} == {SkipReason.PRICE}
    no_markup = catalog.keys() - known.keys()
    assert len(no_markup) == 378
    assert {skip_reason(rows[sku]) for sku in no_markup} == {SkipReason.NO_SALES, SkipReason.COST}
    assert summary.skip[SkipReason.NO_SALES] == sum(skip_reason(rows[sku]) == SkipReason.NO_SALES for sku in no_markup)
    assert round(sum(row.in_transit for row in facts.recommendations)) == 17127
