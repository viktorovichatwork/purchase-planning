from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class Segment(StrEnum):
    DEAD = "Мёртвые"
    SLEEPING = "Спящие"
    REVIEW = "На пересмотр"
    CONDITIONAL = "Условно к закупке"
    PURCHASE = "К закупке"


@dataclass(frozen=True)
class CategoryProfile:
    category: str
    weights: tuple[float, ...]
    season_window: tuple[int, ...]

    @property
    def is_seasonal(self) -> bool:
        return bool(self.season_window)

    def weight_for(self, month: int) -> float:
        return self.weights[month - 1]


@dataclass(frozen=True)
class Recommendation:
    sku_id: str
    supplier_id: str
    order_qty: float
    excluded_stockout_months: int = 0
    monthly_forecast: tuple[float, ...] = ()
    low_data: bool = False
    stock_at_calculation: float = 0.0
    arrival_date: date | None = None
    stock_at_arrival: float | None = None
    in_transit_by_month: tuple[tuple[date, float], ...] = ()
    order_flags: tuple[str, ...] = ()
    segment: Segment = Segment.PURCHASE
    is_new: bool = False
    markup_pct_12m: float | None = None
    sales_qty_12m: float = 0.0
    sku_flags: tuple[str, ...] = ()
    stockout_risk: bool = False


@dataclass(frozen=True)
class SupplierTiming:
    lead_days: int | None
    lead_source: str
    cycle_days: int | None
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class SupplierSummary:
    supplier_id: str
    timing: SupplierTiming
    order_qty: float
    time_to_order: bool


@dataclass(frozen=True)
class PlanFacts:
    cutoff: date
    as_of: date
    recommendations: tuple[Recommendation, ...]
    cutoff_warning: str | None = None
    forecast_months: tuple[date, ...] = ()
    category_profiles: tuple[CategoryProfile, ...] = ()
    suppliers: tuple[SupplierSummary, ...] = ()
    supply_warning: str | None = None

    def __post_init__(self) -> None:
        if any(len(row.monthly_forecast) != len(self.forecast_months) for row in self.recommendations):
            raise ValueError("Число значений прогноза должно совпадать с числом месяцев горизонта.")
