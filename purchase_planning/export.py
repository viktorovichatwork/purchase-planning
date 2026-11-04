import csv
from math import isfinite
from pathlib import Path


REQUIRED_COLUMNS = {
    "sku_dim": (
        "sku_id category brand_id supplier_id supply_type uom weight_kg warranty_months "
        "target_cover_days price_decile first_sale_month last_sale_month months_with_sales "
        "qty_total avg_markup_pct_12m on_order_qty created"
    ),
    "sales_monthly": "sku_id category month qty_sold qty_returned orders_cnt markup_pct partial_month",
    "stock_monthly": "sku_id month stock_qty",
    "purchase_orders": (
        "po_id sku_id supplier_id supply_type order_date promised_date planned_lead_days "
        "status qty_ordered qty_confirmed qty_received qty_pending"
    ),
    "receipts": "sku_id receipt_date qty supplier_id supply_type",
    "supplier_dim": (
        "supplier_id supply_type receipts_cnt skus median_days_between_receipts median_planned_lead_days"
    ),
}


class ExportValidationError(ValueError):
    def __init__(self, reasons: list[str]) -> None:
        self.reasons = tuple(reasons)
        super().__init__("Выгрузка отклонена:\n" + "\n".join(f"- {reason}" for reason in reasons))


def finite_quantity(value: str) -> float:
    quantity = float(value)
    if not isfinite(quantity):
        raise ValueError
    return quantity


def read_export(data_dir: Path) -> dict[str, list[dict[str, str]]]:
    tables: dict[str, list[dict[str, str]]] = {}
    reasons: list[str] = []

    for table, required in REQUIRED_COLUMNS.items():
        path = data_dir / f"{table}.csv"
        try:
            with path.open(encoding="utf-8-sig", newline="") as source:
                reader = csv.reader(source, strict=True)
                header = next(reader, [])
                if not header:
                    reasons.append(f"{path.name}: файл пуст.")
                    continue
                missing = [column for column in required.split() if column not in header]
                if missing:
                    reasons.append(
                        f"{path.name}: отсутствуют обязательные колонки {', '.join(missing)}. "
                        "Проверьте разделитель: нужна запятая."
                    )
                    continue
                if len(set(header)) != len(header):
                    reasons.append(f"{path.name}: повторяются заголовки колонок.")
                    continue

                rows = []
                for values in reader:
                    if not any(value.strip() for value in values):
                        continue
                    if len(values) != len(header):
                        reasons.append(
                            f"{path.name}: строка {reader.line_num}, неверное число полей."
                        )
                        continue
                    rows.append(dict(zip(header, values, strict=True)))
                if not rows:
                    reasons.append(f"{path.name}: нет строк данных.")
                tables[table] = rows
        except UnicodeError:
            reasons.append(f"{path.name}: неверная кодировка, нужен UTF-8.")
        except csv.Error:
            reasons.append(f"{path.name}: некорректный формат CSV.")
        except OSError:
            reasons.append(f"{path.name}: не удалось прочитать файл.")

    if tables.get("sku_dim"):
        known_skus = {row["sku_id"] for row in tables["sku_dim"] if row["sku_id"].strip()}
        if any(not row["sku_id"].strip() for row in tables["sku_dim"]):
            reasons.append("sku_dim.csv: есть пустые sku_id.")
        for table in ("sales_monthly", "stock_monthly", "purchase_orders", "receipts"):
            unknown = {row["sku_id"] for row in tables.get(table, [])} - known_skus
            if unknown:
                ids = ", ".join(repr(sku) for sku in sorted(unknown))
                reasons.append(f"{table}.csv: sku_id вне справочника: {ids}.")

    if reasons:
        raise ExportValidationError(reasons)
    return tables
