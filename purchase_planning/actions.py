"""What the buyer does with each SKU: three groups with a plain reason and a next step."""

from enum import StrEnum

from purchase_planning.demand import DEMAND_WINDOW_MONTHS, LONG_STOCKOUT_MONTHS
from purchase_planning.facts import NO_SUPPLIER_FLAG, TYPICAL_TIMING_FLAG, Recommendation, Segment, SeasonStatus
from purchase_planning.segments import PURCHASE_MARKUP_PCT, RECOVERY_MARKUP_PCT, REVIEW_MARKUP_PCT, ReviewReason


class Action(StrEnum):
    ORDER = "Заказать"
    DECIDE = "Решить закупщику"
    SKIP = "Не заказывать"


ACTION_HINTS = {
    Action.ORDER: "Система посчитала количество и дату. Заказ можно размещать у поставщика.",
    Action.DECIDE: "Количество посчитано, но нужно решение закупщика. У каждого товара указано, почему и что сделать.",
    Action.SKIP: "Система не заказывает: товар не продаётся или его цена требует пересмотра. Причина и шаг — в XLSX.",
}

_ACTIONS = {
    Segment.PURCHASE: Action.ORDER,
    Segment.CONDITIONAL: Action.DECIDE,
    Segment.REVIEW: Action.SKIP,
    Segment.DEAD: Action.SKIP,
    Segment.SLEEPING: Action.SKIP,
}


def action(row: Recommendation) -> Action:
    return _ACTIONS[row.segment]


def _markup(row: Recommendation) -> str:
    return f"{row.markup_pct_12m:.1f}".replace(".", ",") + "%"


def decision_points(row: Recommendation) -> tuple[tuple[str, str], ...]:
    """Pairs of (why, what to do) for SKUs the buyer has to decide on or that are not ordered."""
    points: list[tuple[str, str]] = []
    flags = set(row.sku_flags)
    if row.segment == Segment.DEAD:
        points.append(("Год без продаж, хотя товар был на складе", "Распродать или списать остаток"))
    elif row.segment == Segment.SLEEPING:
        points.append(("Год без продаж и без остатка", "Решить, возвращать ли товар в ассортимент"))
    elif row.segment == Segment.REVIEW:
        if ReviewReason.MISSING_COST in flags:
            points.append(("Себестоимость не проставлена для большинства продаж", "Проставить себестоимость в ERP"))
        elif ReviewReason.LOW_MARKUP in flags:
            points.append((
                f"Наценка {_markup(row)} — ниже {REVIEW_MARKUP_PCT}%", "Поднять цену или вывести из ассортимента",
            ))
        else:
            current = f" ({_markup(row)})" if row.markup_pct_12m is not None else ""
            points.append((
                f"Наценка{current} ещё не вернулась к {RECOVERY_MARKUP_PCT}% после пересмотра",
                f"Поднять цену до {RECOVERY_MARKUP_PCT}% или вывести из ассортимента",
            ))
    elif row.segment == Segment.CONDITIONAL:
        typical = next((flag for flag in row.sku_flags if flag.startswith(TYPICAL_TIMING_FLAG)), None)
        if typical is not None:
            lead = typical.removeprefix(TYPICAL_TIMING_FLAG).strip()
            missing = "Поставщик не указан" if NO_SUPPLIER_FLAG in flags else "Срок поставки неизвестен"
            points.append((
                f"{missing}. Количество посчитано по типичному сроку поставки {lead}",
                "Назначить поставщика и проверить количество",
            ))
        if row.excluded_stockout_months >= LONG_STOCKOUT_MONTHS:
            points.append((
                f"Товара не было {row.excluded_stockout_months} из {DEMAND_WINDOW_MONTHS} мес. — "
                "прогноз по немногим месяцам",
                "Проверить, нужен ли товар и в каком количестве",
            ))
        if row.low_data:
            points.append(("Мало истории продаж — прогноз по похожим товарам", "Проверить количество"))
        if "Мало данных о наценке" in flags:
            points.append(("Наценка неизвестна", "Проверить себестоимость в ERP"))
        if row.markup_pct_12m is not None and row.markup_pct_12m < PURCHASE_MARKUP_PCT:
            points.append((
                f"Наценка {_markup(row)} — ниже {PURCHASE_MARKUP_PCT}%",
                "Согласовать цену или принять низкую наценку",
            ))
        season = row.seasonal_order
        if season is not None and season.status == SeasonStatus.LATE:
            points.append((
                "Сезонный заказ опоздал: придёт после начала сезона и покроет только его остаток",
                "Заказать остаток сезона или пропустить",
            ))
        if season is not None and season.status == SeasonStatus.NOT_RECOMMENDED:
            points.append((
                "Сезонный заказ опоздал, партия на остаток сезона слишком мала",
                "Скорее пропустить сезон",
            ))
        if not points:
            points.append(("Нужна проверка рекомендации", "Проверить количество перед заказом"))
    return tuple(points)


def why_text(row: Recommendation) -> str:
    return " · ".join(why for why, _ in decision_points(row))


def todo_text(row: Recommendation) -> str:
    return " · ".join(dict.fromkeys(todo for _, todo in decision_points(row)))
