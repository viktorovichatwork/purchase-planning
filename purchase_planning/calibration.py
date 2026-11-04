import json
from datetime import date
from math import isfinite
from pathlib import Path

from purchase_planning.facts import BacktestMetric, BacktestQuantiles, BacktestResult, DemandType

BACKTEST_FILENAME = "backtest.json"
REGULAR_SERVICE = 95
INTERMITTENT_SERVICE = 85
SEASONAL_SERVICE = 75
MAX_STOCK_MULTIPLIER = 2
METRIC_HEADERS = ("Разрез", "Группа", "Окон", "WAPE", "Смещение", "WAPE среднего за 12 мес", "Смещение среднего за 12 мес")


def read_backtest(directory: Path, as_of: date) -> tuple[BacktestResult | None, str | None]:
    try:
        data = json.loads((directory / BACKTEST_FILENAME).read_text(encoding="utf-8"))
        cutoff = date.fromisoformat(data["cutoff"])
        if cutoff > as_of:
            return None, "Дата среза бэктеста позже даты расчёта. Используется целевой запас по прогнозу."
        quantiles = {DemandType(key): BacktestQuantiles(**value) for key, value in data["quantiles"].items()}
        for group in quantiles.values():
            if group.windows < 1 or not (
                0 <= group.p75 <= group.p85 <= group.p95 and isfinite(group.p95)
            ):
                raise ValueError
        metrics = tuple(BacktestMetric(**value) for value in data["metrics"])
        for metric in metrics:
            values = (metric.wape, metric.bias, metric.baseline_wape, metric.baseline_bias)
            if (
                not isinstance(metric.breakdown, str)
                or not isinstance(metric.group, str)
                or not isinstance(metric.windows, int)
                or metric.windows < 0
                or any(value is not None and not isfinite(value) for value in values)
            ):
                raise ValueError
        warning = None
        if quantiles.keys() != set(DemandType):
            warning = "Для групп без квантилей целевой запас равен прогнозу."
        return BacktestResult(
            cutoff, tuple(date.fromisoformat(value) for value in data["origins"]),
            metrics, quantiles,
        ), warning
    except FileNotFoundError:
        return None, "Нет backtest.json. Используется целевой запас по прогнозу."
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None, "Не удалось прочитать backtest.json. Используется целевой запас по прогнозу."


def stock_multiplier(backtest: BacktestResult | None, demand_type: DemandType, seasonal: bool) -> float:
    quantiles = backtest.quantiles.get(demand_type) if backtest else None
    if quantiles is None:
        return 1.0
    if seasonal:
        quantile = quantiles.p75
    elif demand_type == DemandType.REGULAR:
        quantile = quantiles.p95
    else:
        quantile = quantiles.p85
    return min(quantile, MAX_STOCK_MULTIPLIER)
