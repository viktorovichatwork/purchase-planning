from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Recommendation:
    sku_id: str
    supplier_id: str
    order_qty: float


@dataclass(frozen=True)
class PlanFacts:
    cutoff: date
    as_of: date
    recommendations: tuple[Recommendation, ...]
    cutoff_warning: str | None = None
