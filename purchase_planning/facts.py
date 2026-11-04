from dataclasses import dataclass
from datetime import date


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


@dataclass(frozen=True)
class PlanFacts:
    cutoff: date
    as_of: date
    recommendations: tuple[Recommendation, ...]
    cutoff_warning: str | None = None
    forecast_months: tuple[date, ...] = ()
    category_profiles: tuple[CategoryProfile, ...] = ()

    def __post_init__(self) -> None:
        if any(len(row.monthly_forecast) != len(self.forecast_months) for row in self.recommendations):
            raise ValueError("Число значений прогноза должно совпадать с числом месяцев горизонта.")
