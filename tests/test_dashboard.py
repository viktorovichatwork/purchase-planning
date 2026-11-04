from datetime import date
from xml.etree import ElementTree

from openpyxl import load_workbook

from purchase_planning.dashboard import write_dashboard
from purchase_planning.facts import PlanFacts, Recommendation, Segment, SupplierSummary, SupplierTiming
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
    order_rows = page.findall(".//table/tbody/tr")
    workbook = load_workbook(report)
    try:
        totals = [(row[0], row[2]) for row in workbook.active.values if row[1] == "Итого"]
        assert [(row[0].text, int(row[2].text)) for row in order_rows] == totals == [("А<&", 9), ("Б", 1)]
    finally:
        workbook.close()
    assert [row[1].text for row in order_rows] == ["Сейчас", "01.10.2026"]
    assert page.find(".//a").attrib["href"] == "report.xlsx"
    visible_text = "".join(page.itertext())
    for value in ("13.08.2026", "36 дн.", "Черновик, не размещать", "Первый запуск",
                  "Зависший заказ", "Хвост заказа", "Условно к закупке"):
        assert value in visible_text
