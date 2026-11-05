from datetime import date, timedelta

import pytest
from openpyxl import load_workbook

from purchase_planning.facts import PlanFacts, Recommendation, Segment, SupplierSummary, SupplierTiming
from purchase_planning.report import write_report


@pytest.mark.parametrize(
    ("run_date", "status"),
    [
        (date(2026, 9, 17), None),
        (date(2026, 9, 18), "Устаревшие данные. Перед заказом загрузите свежую выгрузку ERP."),
    ],
)
def test_order_sheet_uses_facts_and_marks_stale_exports(tmp_path, run_date, status):
    facts = PlanFacts(
        cutoff=date(2026, 8, 13),
        as_of=date(2026, 8, 1),
        recommendations=(Recommendation("SKU-1", "SUP-1", 25),),
    )
    path = tmp_path / "order.xlsx"

    write_report(facts, path, run_date=run_date)

    workbook = load_workbook(path)
    try:
        sheet = workbook["Заказ по поставщикам"]
        assert sheet["B2"].value == status
        assert list(sheet.values)[-2][:6] == ("SUP-1", "SKU-1", None, 25, 0, 0)
    finally:
        workbook.close()


def test_order_sheet_prioritizes_earliest_batches_and_sums_rounded_lines(tmp_path):
    day = date(2026, 8, 13)
    timing = SupplierTiming(140, "История", 120, ("цикл ограничен (было 200 дней)",))
    facts = PlanFacts(
        day, day,
        (
            Recommendation("SKU-1", "SUP-A", 12),
            Recommendation("SKU-2", "SUP-B", 2.1),
            Recommendation("SKU-3", "SUP-B", 5.1, segment=Segment.CONDITIONAL),
            Recommendation("SKU-4", "SUP-B", 0),
        ),
        # The old cycle-lookahead flag must not take priority over the actual deadline.
        suppliers=(SupplierSummary("SUP-A", timing, 12, True, day + timedelta(days=10)),
                   SupplierSummary("SUP-B", timing, 2.1, True, day + timedelta(days=2))),
    )
    path = tmp_path / "order.xlsx"
    write_report(facts, path, run_date=day)

    workbook = load_workbook(path)
    try:
        _, _, header, *rows = workbook["Заказ по поставщикам"].values
        assert [(row[0], row[1], row[header.index("К заказу")]) for row in rows] == [
            ("SUP-B", "SKU-3", 6), ("SUP-B", "SKU-2", 3), ("SUP-B", "Итого", 9),
            ("SUP-A", "SKU-1", 12), ("SUP-A", "Итого", 12),
        ]
        assert rows[0][header.index("Флаги поставщика")] == timing.flags[0]
        assert rows[0][header.index("Дата заказа у поставщика")] == "2026-08-15"
        assert rows[0][header.index("Пора заказывать у поставщика")] == "Нет"
        assert facts.recommendations[2].order_qty == 5.1
        for name in ("Заказ по поставщикам", "Решить закупщику", "Не заказывать"):
            target = workbook[name]
            for label in ("Наценка за 12 мес, %", "Продажи за 12 мес, шт."):
                cell = next(cell for cells in target for cell in cells if cell.value == label)
                assert "12 полных месяцев плюс текущий неполный месяц" in cell.comment.text
    finally:
        workbook.close()
