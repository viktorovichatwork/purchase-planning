import csv
from pathlib import Path


CATEGORY_NAMES = {
    "space_heaters": "Обогреватели",
    "string_trimmers": "Триммеры",
    "trimmer_blades_accessories": "Лески и ножи",
    "sanitary_faucets": "Смесители",
    "concrete_mixers": "Бетономешалки",
    "grain_mills": "Зернодробилки",
}


def category_name(category: str) -> str:
    return CATEGORY_NAMES.get(category, category)


def load_sku_labels(data_dir: str | Path) -> dict[str, str]:
    """Build display labels from the SKU catalog in a validated export."""
    labels = {}
    with (Path(data_dir) / "sku_dim.csv").open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            category = category_name(row["category"].strip())
            brand = row["brand_id"].strip()
            try:
                decile = float(row["price_decile"])
            except ValueError:
                decile = None
            price = f"ценовая группа {int(decile)} из 10" if decile is not None and decile in range(1, 11) else ""
            labels[row["sku_id"]] = " · ".join(part for part in (category, brand, price) if part)
    return labels
