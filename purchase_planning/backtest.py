import argparse
import json
import sys
from calendar import monthrange
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import asdict
from datetime import date, timedelta
from math import fsum
from pathlib import Path
from statistics import quantiles
from tempfile import TemporaryDirectory

from purchase_planning.calibration import BACKTEST_FILENAME
from purchase_planning.demand import is_stockout_month
from purchase_planning.export import ExportValidationError, read_export
from purchase_planning.facts import BacktestMetric, BacktestQuantiles, BacktestResult, DemandType
from purchase_planning.inventory import month_shares
from purchase_planning.planner import _read_cutoff, plan, read_monthly_history
from purchase_planning.supply import read_supply_history

FIRST_ORIGIN = date(2024, 6, 30)
BASELINE_MONTHS = 12
DAY = timedelta(days=1)


def _month_ends(first: date, last: date) -> Iterator[date]:
    for month, _ in month_shares(first.replace(day=1), last + DAY):
        yield month.replace(day=monthrange(month.year, month.month)[1])


def _metrics(breakdown: str, group: str, windows: list[tuple[float, float, float]]) -> BacktestMetric:
    actual = fsum(actual for actual, _, _ in windows)
    if actual == 0:
        return BacktestMetric(breakdown, group, len(windows), None, None, None, None)
    return BacktestMetric(
        breakdown, group, len(windows),
        fsum(abs(forecast - actual) for actual, forecast, _ in windows) / actual,
        fsum(forecast - actual for actual, forecast, _ in windows) / actual,
        fsum(abs(baseline - actual) for actual, _, baseline in windows) / actual,
        fsum(baseline - actual for actual, _, baseline in windows) / actual,
    )


def run_backtest(data_dir: str | Path) -> BacktestResult:
    directory = Path(data_dir)
    tables = read_export(directory)
    sales_rows, stock, partial_months = read_monthly_history(tables)
    orders, receipts = read_supply_history(tables)
    cutoff, _ = _read_cutoff(
        directory / "export_meta.json", [sale.month for _, sale in sales_rows if sale.month not in partial_months],
        orders, receipts,
    )
    sales: dict[tuple[str, date], float] = defaultdict(float)
    for sku, sale in sales_rows:
        if sale.month <= cutoff and sale.month not in partial_months:
            sales[sku, sale.month] += sale.quantity

    windows: dict[tuple[str, str], list[tuple[float, float, float]]] = defaultdict(list)
    ratios: dict[DemandType, list[float]] = defaultdict(list)
    origins = []
    for origin in _month_ends(FIRST_ORIGIN, cutoff):
        if origin >= cutoff or origin in partial_months:
            continue
        facts = plan(directory, origin)
        recent_months = tuple(_month_ends(date(origin.year - 1, origin.month, 1), origin))[-BASELINE_MONTHS:]
        baseline_months = tuple(month for month in recent_months if month not in partial_months)
        horizons = {}
        for supplier in facts.suppliers:
            horizon_days = supplier.timing.horizon_days
            if horizon_days is None:
                continue
            end = origin + timedelta(days=horizon_days)
            # A fraction of a month can be scored only when its full actual is known.
            shares = tuple(
                (month.replace(day=monthrange(month.year, month.month)[1]), share)
                for month, share in month_shares(origin + DAY, end + DAY)
            )
            if all(month <= cutoff and month not in partial_months for month, _ in shares):
                horizons[supplier.supplier_id] = shares

        count = len(windows["Все", "Все"])
        for row in facts.recommendations:
            shares = horizons.get(row.supplier_id, ())
            if not shares:
                continue
            sku = row.sku_id
            if any(is_stockout_month(stock.get((sku, month), 0.0), sales[sku, month]) for month, _ in shares):
                continue
            actual = fsum(sales[sku, month] * share for month, share in shares)
            forecast = fsum(row.monthly_forecast)
            baseline = (
                fsum(sales[sku, month] for month in baseline_months) / len(baseline_months)
                * fsum(share for _, share in shares)
            )
            window = (actual, forecast, baseline)
            windows["Все", "Все"].append(window)
            windows["Категория", row.category].append(window)
            windows["Тип спроса", row.demand_type].append(window)
            if forecast > 0:
                ratios[row.demand_type].append(actual / forecast)
        if len(windows["Все", "Все"]) > count:
            origins.append(origin)

    group_quantiles = {}
    for group, values in sorted(ratios.items()):
        percentiles = quantiles(values, n=100, method="inclusive") if len(values) > 1 else values * 99
        group_quantiles[group] = BacktestQuantiles(len(values), percentiles[74], percentiles[84], percentiles[94])
    return BacktestResult(
        cutoff, tuple(origins), tuple(_metrics(*key, values) for key, values in sorted(windows.items())), group_quantiles,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Сохранить метрики и квантили скользящего бэктеста рядом с выгрузкой.")
    parser.add_argument("data_dir", type=Path, help="Каталог выгрузки ERP")
    args = parser.parse_args()
    path = args.data_dir / BACKTEST_FILENAME
    try:
        result = run_backtest(args.data_dir)
        with TemporaryDirectory(dir=path.parent, prefix=".backtest-") as pending:
            staging = Path(pending) / path.name
            staging.write_text(
                json.dumps(asdict(result), default=date.isoformat, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                encoding="utf-8",
            )
            staging.replace(path)
    except (ExportValidationError, OSError, ValueError) as error:
        print(f"Бэктест прерван. {error}", file=sys.stderr)
        return 1
    print(f"Бэктест сохранён: {path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
