from datetime import date

from purchase_planning.facts import PlanFacts, Recommendation
from purchase_planning.wording import days_text

def order_date_label(
    day: date, as_of: date, *, date_format: str = "%Y-%m-%d", below_reorder_point: bool = False,
) -> str:
    if day > as_of:
        return day.strftime(date_format)
    return "Сейчас · запас ниже точки заказа" if below_reorder_point else "Сейчас"


def open_orders_text(row: Recommendation, as_of: date) -> str:
    """Open supplier orders in plain words: when each one arrives or how late it is."""
    parts = []
    for promised, quantity in row.open_orders:
        amount = f"{quantity:.0f} шт."
        if promised is None:
            parts.append(f"{amount} — в ERP нет верной даты прихода, в расчёт не взято")
        elif promised == as_of:
            parts.append(f"{amount} — придёт {promised:%d.%m.%Y}, в день выгрузки")
        elif promised > as_of:
            parts.append(f"{amount} — придёт {promised:%d.%m.%Y}, через {days_text((promised - as_of).days)}")
        else:
            parts.append(f"{amount} — опаздывает на {days_text((as_of - promised).days)}, в расчёт не взято")
    return "; ".join(parts) or "—"


def coverage_text(row: Recommendation) -> str:
    if row.coverage_months is None:
        return "Нет прогноза"
    prefix = "> " if row.stock_until is None else ""
    return prefix + f"{row.coverage_months:.1f}".replace(".", ",")


def stockout_rows(facts: PlanFacts) -> list[Recommendation]:
    return sorted(
        (row for row in facts.recommendations if row.stockout_risk),
        key=lambda row: (row.stock_until or date.max, -row.lost_sales, row.sku_id),
    )


def excess_rows(facts: PlanFacts) -> list[Recommendation]:
    return sorted(
        (row for row in facts.recommendations if row.excess),
        key=lambda row: (-(row.coverage_months or 0) * (row.price_decile or 0), row.sku_id),
    )
