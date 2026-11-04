"""What the buyer does with each SKU: three groups with a plain reason and a next step."""

from enum import StrEnum

from purchase_planning.demand import LONG_STOCKOUT_SHARE
from purchase_planning.facts import NO_SUPPLIER_FLAG, TYPICAL_TIMING_FLAG, Recommendation, Segment, SeasonStatus
from purchase_planning import segments
from purchase_planning.segments import ReviewReason


class Action(StrEnum):
    ORDER = "Заказать"
    DECIDE = "Решить закупщику"
    SKIP = "Не заказывать"


class SkipReason(StrEnum):
    """Why a SKU is not ordered: three separate lists for the buyer."""

    PRICE = "Пересмотреть цену"
    COST = "Проставить себестоимость"
    NO_SALES = "Год без продаж"


ACTION_HINTS = {
    Action.ORDER: "Система посчитала количество и дату. Заказ можно размещать у поставщика.",
    Action.DECIDE: "Количество посчитано, но нужно решение закупщика. У каждого товара указано, почему и что сделать.",
    Action.SKIP: "Система не заказывает: наценка ниже порога, себестоимость не проставлена или год не было продаж. "
                 "Три списка с причиной и шагом — во вкладке «Не заказывать».",
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


def segment_action(segment: str | None) -> Action | None:
    return _ACTIONS[Segment(segment)] if segment is not None else None


def low_markup(row: Recommendation) -> bool:
    return row.markup_pct_12m is not None and row.markup_pct_12m < segments.PURCHASE_MARKUP_PCT


def skip_reason(row: Recommendation) -> SkipReason | None:
    if action(row) != Action.SKIP:
        return None
    if row.segment in {Segment.DEAD, Segment.SLEEPING}:
        return SkipReason.NO_SALES
    return SkipReason.PRICE if low_markup(row) else SkipReason.COST


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
        if low_markup(row):
            points.append((
                f"Наценка {_markup(row)} — ниже {segments.PURCHASE_MARKUP_PCT}%",
                f"Поднять цену до наценки {segments.PURCHASE_MARKUP_PCT}% или вывести из ассортимента",
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
        checked, missing = row.checked_stock_months, row.missing_stock_months
        if checked and missing >= LONG_STOCKOUT_SHARE * checked:
            months = "сезонных мес." if row.seasonal_category else "мес."
            points.append((
                f"Товара не было на складе {missing} из {checked} {months} — прогноз по немногим месяцам",
                "Проверить, нужен ли товар и в каком количестве",
            ))
        if row.low_data:
            points.append(("Мало истории продаж — прогноз по похожим товарам", "Проверить количество"))
        if "Мало данных о наценке" in flags:
            points.append(("Наценка неизвестна", "Проверить себестоимость в ERP"))
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
