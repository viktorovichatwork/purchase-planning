import csv
from datetime import date

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
        "receipts": "sku_id,receipt_date,qty,supplier_id,supply_type\nSKU-1,2026-07-01,10,SUP-1,import\n",
        "supplier_dim": (
            "supplier_id,supply_type,receipts_cnt,skus,median_days_between_receipts,"
            "median_planned_lead_days\nSUP-1,import,1,1,,181\n"
        ),
    }
    for name, contents in tables.items():
        (tmp_path / f"{name}.csv").write_text(contents, encoding="utf-8")
    (tmp_path / "export_meta.json").write_text('{"cutoff_date":"2026-08-13"}', encoding="utf-8")
    return tmp_path


def test_export_to_order_sheet(export):
    facts = plan(export)
    assert facts == plan(export)
    assert facts.as_of == facts.cutoff == date(2026, 8, 13)
    assert len(facts.recommendations) == 1
    row = facts.recommendations[0]
    assert (row.supplier_id, row.sku_id, row.order_qty) == ("SUP-1", "SKU-1", 95)

    path = export / "order.xlsx"
    write_report(facts, path, run_date=date(2026, 8, 14))
    workbook = load_workbook(path)
    try:
        assert list(workbook["Заказ по поставщикам"].values)[-1] == (
            row.supplier_id, row.sku_id, row.order_qty
        )
    finally:
        workbook.close()


def test_missing_metadata_uses_last_complete_month_and_warns_in_report(export):
    (export / "export_meta.json").unlink()
    facts = plan(export)
    assert facts.as_of == facts.cutoff == date(2026, 7, 31)
    assert facts.recommendations[0].order_qty == 105

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
    assert facts.recommendations[0].order_qty == 45


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
