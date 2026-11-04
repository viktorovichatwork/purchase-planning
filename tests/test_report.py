from datetime import date

import pytest
from openpyxl import load_workbook

from purchase_planning.facts import PlanFacts, Recommendation
from purchase_planning.report import write_report


@pytest.mark.parametrize(
    ("run_date", "status"),
    [
        (date(2026, 9, 17), None),
        (date(2026, 9, 18), "черновик, не размещать"),
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
        assert list(sheet.values)[-1][:5] == ("SUP-1", "SKU-1", 25, 0, None)
    finally:
        workbook.close()
