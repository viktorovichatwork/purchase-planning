"""What the buyer does with each SKU: three groups with a plain reason and a next step."""

from enum import StrEnum

from purchase_planning.demand import LONG_STOCKOUT_SHARE
from purchase_planning.facts import (
    NO_SUPPLIER_FLAG, TYPICAL_TIMING_FLAG, TYPICAL_TIMING_SOURCE, DecisionFacts, DecisionReason,
    Recommendation, Segment, SeasonStatus, SupplierTiming,
)
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


def decision_facts(row: Recommendation, typical_timing: SupplierTiming | None = None) -> DecisionFacts:
    """Prefer recorded provenance. Interpret flags only for pre-provenance callers."""
    if row.decision is not None:
        return row.decision
    reasons = []
    flags = set(row.sku_flags)
    if ReviewReason.MISSING_COST in flags:
        reasons.append(DecisionReason.MISSING_COST)
    if row.markup_pct_12m is not None and row.markup_pct_12m < segments.PURCHASE_MARKUP_PCT:
        reasons.append(DecisionReason.LOW_MARKUP)
    if row.low_data:
        reasons.append(DecisionReason.LOW_DATA)
    if "Мало данных о наценке" in flags:
        reasons.append(DecisionReason.MISSING_MARKUP)
    if NO_SUPPLIER_FLAG in flags:
        reasons.append(DecisionReason.MISSING_SUPPLIER)
    typical = next((flag for flag in row.sku_flags if flag.startswith(TYPICAL_TIMING_FLAG)), None)
    if typical is not None:
        reasons.append(DecisionReason.TYPICAL_TIMING)
        if typical_timing is None:
            try:
                lead = int(typical.removeprefix(TYPICAL_TIMING_FLAG).split()[0])
            except (ValueError, IndexError):
                lead = None
            typical_timing = SupplierTiming(lead, TYPICAL_TIMING_SOURCE, None)
    else:
        typical_timing = None
    if row.checked_stock_months and row.missing_stock_months >= LONG_STOCKOUT_SHARE * row.checked_stock_months:
        reasons.append(DecisionReason.LONG_STOCKOUT)
    if row.seasonal_order is not None:
        if row.seasonal_order.status == SeasonStatus.LATE:
            reasons.append(DecisionReason.LATE_SEASON)
        elif row.seasonal_order.status == SeasonStatus.NOT_RECOMMENDED:
            reasons.append(DecisionReason.SMALL_LATE_SEASON)
    return DecisionFacts(tuple(reasons), segments.PURCHASE_MARKUP_PCT, typical_timing)


def low_markup(row: Recommendation) -> bool:
    return DecisionReason.LOW_MARKUP in decision_facts(row).reasons


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
    decision = decision_facts(row)
    reasons = decision.reasons
    threshold = f"{decision.markup_threshold:g}"
    if row.segment == Segment.DEAD:
        points.append(("Год без продаж, хотя товар был на складе", "Распродать или списать остаток"))
    elif row.segment == Segment.SLEEPING:
        points.append(("Год без продаж и без остатка", "Решить, возвращать ли товар в ассортимент"))
    elif row.segment == Segment.REVIEW:
        if DecisionReason.MISSING_COST in reasons:
            points.append(("Себестоимость не проставлена для большинства продаж", "Проставить себестоимость в ERP"))
        if DecisionReason.LOW_MARKUP in reasons:
            points.append((
                f"Наценка {_markup(row)} — ниже {threshold}%",
                f"Поднять цену до наценки {threshold}% или вывести из ассортимента",
            ))
    elif row.segment == Segment.CONDITIONAL:
        if DecisionReason.TYPICAL_TIMING in reasons:
            timing = decision.typical_timing
            lead = f"{timing.lead_days} дн." if timing is not None and timing.lead_days is not None else ""
            missing = "Поставщик не указан" if DecisionReason.MISSING_SUPPLIER in reasons else "Срок поставки неизвестен"
            points.append((
                f"{missing}. Количество посчитано по типичному сроку поставки {lead}",
                "Назначить поставщика и проверить количество",
            ))
        checked, missing = row.checked_stock_months, row.missing_stock_months
        if DecisionReason.LONG_STOCKOUT in reasons:
            months = "сезонных мес." if row.seasonal_category else "мес."
            points.append((
                f"Товара не было на складе {missing} из {checked} {months} — прогноз по немногим месяцам",
                "Проверить, нужен ли товар и в каком количестве",
            ))
        if DecisionReason.LOW_DATA in reasons:
            points.append(("Мало истории продаж — прогноз по похожим товарам", "Проверить количество"))
        if DecisionReason.MISSING_MARKUP in reasons:
            points.append(("Наценка неизвестна", "Проверить себестоимость в ERP"))
        if DecisionReason.LATE_SEASON in reasons:
            points.append((
                "Сезонный заказ опоздал: придёт после начала сезона и покроет только его остаток",
                "Заказать остаток сезона или пропустить",
            ))
        if DecisionReason.SMALL_LATE_SEASON in reasons:
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
