from datetime import date
from xml.etree import ElementTree

from openpyxl import load_workbook

from purchase_planning.dashboard import write_dashboard
from purchase_planning.explanations import Explanations
from purchase_planning.facts import OrderFlag, PlanFacts, Recommendation, Segment, SupplierSummary, SupplierTiming
from purchase_planning.report import write_report


def test_dashboard_matches_workbook_and_displays_facts(tmp_path):
    cutoff = date(2026, 8, 13)
    run_date = date(2026, 9, 18)
    timing = SupplierTiming(140, "История", 90)
    facts = PlanFacts(
        cutoff, cutoff,
        (
            Recommendation("SKU-1", "А<&", 2.1, order_flags=("Хвост заказа",)),
            Recommendation("SKU-2", "А<&", 5.1, segment=Segment.CONDITIONAL),
            Recommendation("SKU-3", "Б", 1, order_flags=("Зависший заказ",)),
        ),
        suppliers=(SupplierSummary("Б", timing, 1, False, date(2026, 10, 1)),
                   SupplierSummary("А<&", timing, 7.2, True)),
    )
    report = tmp_path / "report.xlsx"
    dashboard = tmp_path / "index.html"
    write_report(facts, report, run_date=run_date)
    write_dashboard(facts, dashboard, run_date=run_date, previous_run=None, changes=[])

    page = ElementTree.fromstring(dashboard.read_text(encoding="utf-8").split("\n", 1)[1])
    suppliers = page.findall(".//details/summary")
    workbook = load_workbook(report)
    try:
        totals = [(row[0], row[2]) for row in workbook.active.values if row[1] == "Итого"]
        assert [(row[0].text, int(row[2].text.split()[0])) for row in suppliers] == totals == [("А<&", 9), ("Б", 1)]
    finally:
        workbook.close()
    assert [row[1].text for row in suppliers] == ["Сейчас · пора заказывать", "01.10.2026"]
    assert len(page.findall(".//details/ul/li")) == 3
    assert page.findall(".//details/ul/li/dl/dt")[0].text == "К заказу, шт."
    assert page.find(".//a").attrib["href"] == "report.xlsx"
    visible_text = "".join(page.itertext())
    for value in ("13.08.2026", "36 дн.", "Черновик, не размещать", "Первый запуск",
                  "Зависший заказ", "Хвост заказа", "Условно к закупке"):
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
        assert [(row[0].text, int(row[-1].text)) for row in rows] == [*expected[:2], expected[-1]]
        assert workbook["Избыток"]["F2"].value == 12.3
        assert page.find(".//div[@aria-label='Избыток']/table/tbody/tr/td[5]").text == "12,3"
    finally:
        workbook.close()
    visible_text = "".join(page.itertext())
    assert "ещё 1 незначительных" in visible_text
    assert "Пояснение LLM · Сводка <&" in visible_text
    assert "Пояснение LLM · К заказу 1 шт." in visible_text
