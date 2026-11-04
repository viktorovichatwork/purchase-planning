from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class Segment(StrEnum):
    DEAD = "Мёртвые"
    SLEEPING = "Спящие"
    REVIEW = "На пересмотр"
    CONDITIONAL = "Условно к закупке"
    PURCHASE = "К закупке"


class OrderFlag(StrEnum):
    STUCK = "Зависший заказ"
    TAIL = "Хвост заказа"
    RESCHEDULE = "Перенести/отменить открытый заказ"


class DemandType(StrEnum):
    REGULAR = "Регулярный"
    INTERMITTENT = "Прерывистый"


class SeasonStatus(StrEnum):
    ON_TIME = "Сезонный"
    LATE = "Поздний"
    NOT_RECOMMENDED = "Не рекомендуется"
    MISSED = "Приход после окна"


@dataclass(frozen=True)
class BacktestMetric:
    breakdown: str
    group: str
    windows: int
    wape: float | None
    bias: float | None
    baseline_wape: float | None
    baseline_bias: float | None


@dataclass(frozen=True)
class BacktestQuantiles:
    windows: int
    p75: float
    p85: float
    p95: float


@dataclass(frozen=True)
class BacktestResult:
    cutoff: date
    origins: tuple[date, ...]
    metrics: tuple[BacktestMetric, ...]
    quantiles: dict[DemandType, BacktestQuantiles]


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
class SeasonalOrder:
    window_start: date
    window_end: date
    deadline: date
    days_late: int
    months_remaining: float
    order_qty: float
    ordinary_service_qty: float
    stock_at_season_end: float
    next_deadline: date
    status: SeasonStatus
    stock_at_arrival: float
    target_stock: float
    safety_stock: float

    @property
    def actionable(self) -> bool:
        return self.order_qty > 0 and self.status in {SeasonStatus.ON_TIME, SeasonStatus.LATE}

    @property
    def expedite(self) -> bool:
        return self.days_late > 0


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
    annual_demand_qty: float = 0.0
    category: str = ""
    demand_type: DemandType = DemandType.INTERMITTENT
    target_stock: float | None = None
    safety_stock: float | None = None
    order_multiple: int = 1
    coverage_months: float | None = None
    stock_until: date | None = None
    order_by: date | None = None
    stockout_days: int = 0
    lost_sales: float = 0.0
    excess_threshold: float | None = None
    excess: bool = False
    price_decile: int | None = None
    seasonal_order: SeasonalOrder | None = None
    regular_order_qty: float | None = None
    regular_order_date: date | None = None
    grouped_season_deadline: date | None = None

    @property
    def in_transit(self) -> float:
        return sum(quantity for _, quantity in self.in_transit_by_month)


@dataclass(frozen=True)
class SupplierTiming:
    lead_days: int | None
    lead_source: str
    cycle_days: int | None
    flags: tuple[str, ...] = ()

    @property
    def horizon_days(self) -> int | None:
        if self.lead_days is None or self.cycle_days is None:
            return None
        return self.lead_days + self.cycle_days


@dataclass(frozen=True)
class SupplierSummary:
    supplier_id: str
    timing: SupplierTiming
    order_qty: float
    time_to_order: bool
    next_order_date: date | None = None


@dataclass(frozen=True)
class PlanFacts:
    cutoff: date
    as_of: date
    recommendations: tuple[Recommendation, ...]
    cutoff_source: str | None = None
    forecast_months: tuple[date, ...] = ()
    category_profiles: tuple[CategoryProfile, ...] = ()
    suppliers: tuple[SupplierSummary, ...] = ()
    supply_warning: str | None = None
    backtest: BacktestResult | None = None
    backtest_warning: str | None = None

    def __post_init__(self) -> None:
        if any(len(row.monthly_forecast) != len(self.forecast_months) for row in self.recommendations):
            raise ValueError("Число значений прогноза должно совпадать с числом месяцев горизонта.")
