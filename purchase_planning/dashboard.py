from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape
from math import fsum
from pathlib import Path

from purchase_planning.comparison import Change, summarize
from purchase_planning.explanations import Explanations
from purchase_planning.facts import OrderFlag, PlanFacts, Recommendation, Segment
from purchase_planning.order_sheet import (
    coverage_text, displayed_order_qty, excess_rows, order_date_label, rounded_quantity, stock_below_reorder_point,
    stockout_rows, supplier_orders,
)
from purchase_planning.parameters import STALE_EXPORT_DAYS
from purchase_planning.replenishment import inventory_summary
from purchase_planning.wording import days_text


@dataclass(frozen=True)
class Batch:
    deadline: date
    quantity: int
    seasonal: bool
    low_stock: bool


@dataclass(frozen=True)
class SupplierSchedule:
    supplier_id: str
    lines: list[Recommendation]
    batches: list[Batch]
    flags: tuple[str, ...]

    @property
    def deadline(self) -> date:
        return min(batch.deadline for batch in self.batches)


def _supplier_schedules(facts: PlanFacts) -> list[SupplierSchedule]:
    summaries = {supplier.supplier_id: supplier for supplier in facts.suppliers}
    schedules = []
    for supplier, lines, _ in supplier_orders(facts):
        summary = summaries.get(supplier)
        batches = []
        for row in lines:
            low_stock = stock_below_reorder_point(row)
            season = row.seasonal_order
            seasonal_qty = 0
            if season and season.actionable:
                seasonal_qty = rounded_quantity(season.order_qty, row.order_multiple)
                batches.append(Batch(season.deadline, seasonal_qty, True, low_stock))
            ordinary_qty = displayed_order_qty(row) - seasonal_qty
            if ordinary_qty > 0:
                placement = row.order_by if row.grouped_season_deadline else row.regular_order_date
                if row.regular_order_qty is None:
                    placement = placement or row.order_by or (summary.next_order_date if summary else None)
                batches.append(Batch(placement or facts.as_of, ordinary_qty, False, low_stock))
        schedules.append(SupplierSchedule(supplier, lines, batches, summary.timing.flags if summary else ()))
    return sorted(schedules, key=lambda row: (row.deadline, row.supplier_id))


def _date(day: date | None) -> str:
    return f"{day:%d.%m.%Y}" if day else "—"


def _decimal(value: float | None) -> str:
    return f"{value:.1f}".replace(".", ",") if value is not None else "—"


def _table(label: str, headers: tuple[str, ...], rows: Iterable[tuple[object, ...]]) -> str:
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(str(value))}</td>" for value in row) + "</tr>"
        for row in rows
    )
    if not body:
        return "<p>Нет позиций.</p>"
    heading = "".join(f'<th scope="col">{header}</th>' for header in headers)
    return (
        f'<div class="table-scroll" role="region" aria-label="{label}" tabindex="0">'
        f"<table><thead><tr>{heading}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _sku(row: Recommendation, facts: PlanFacts, explanation: str, supplier_flags: tuple[str, ...]) -> str:
    fields = (
        ("К заказу, шт.", displayed_order_qty(row)),
        ("Остаток, шт.", round(row.stock_at_calculation)),
        ("В пути, шт.", round(row.in_transit)),
        ("Хватит до", _date(row.stock_until) if row.stock_until else (
            "Дольше горизонта" if row.coverage_months is not None else "Нет прогноза"
        )),
        ("Заказать не позже", order_date_label(
            row.order_by, facts.as_of, date_format="%d.%m.%Y", below_reorder_point=stock_below_reorder_point(row),
        ) if row.order_by else "—"),
        ("Покрытие, мес", coverage_text(row)),
        ("Сегмент", row.segment),
        ("Флаги", ", ".join(dict.fromkeys((*row.order_flags, *row.sku_flags, *supplier_flags))) or "—"),
    )
    fields_html = "".join(f"<dt>{label}</dt><dd>{escape(str(value))}</dd>" for label, value in fields)
    why = f"<p><strong>Пояснение LLM</strong> · {escape(explanation)}</p>" if explanation else ""
    return f"<li><strong>{escape(row.sku_id)}</strong><dl>{fields_html}</dl>{why}</li>"


def _order_table(facts: PlanFacts, schedules: list[SupplierSchedule], explanations: dict[str, str]) -> str:
    rows = []
    for schedule in schedules:
        as_of = facts.as_of
        low_stock = any(batch.deadline <= as_of and batch.low_stock for batch in schedule.batches)
        when = order_date_label(schedule.deadline, as_of, date_format="%d.%m.%Y", below_reorder_point=low_stock)
        name = escape(schedule.supplier_id or "Поставщик не указан")
        batch_lines = []
        for seasonal, label in ((False, "Обычная партия"), (True, "Сезонная партия")):
            batches = [batch for batch in schedule.batches if batch.seasonal == seasonal]
            if batches:
                deadline = min(batch.deadline for batch in batches)
                batch_date = f"до {_date(deadline)}" if seasonal else order_date_label(deadline, as_of, date_format="%d.%m.%Y")
                quantity = sum(batch.quantity for batch in batches)
                batch_lines.append(f'<span class="batch">{label} — {batch_date} — {quantity} шт.</span>')
        skus = "".join(_sku(row, facts, explanations.get(row.sku_id, ""), schedule.flags) for row in schedule.lines)
        rows.append(
            f'<details><summary><strong>{name}</strong> <span>Когда заказать — {when}</span>'
            f'{"".join(batch_lines)}</summary><ul class="skus">{skus}</ul></details>'
        )
    if not rows:
        return "<p>Нет позиций с положительным количеством к заказу.</p>"
    return (
        '<p class="meta">Для каждого вида партии показаны ближайший срок и общий объём. '
        'Откройте поставщика, чтобы увидеть SKU.</p>' + "\n".join(rows)
    )


def _decision(
    facts: PlanFacts, schedules: list[SupplierSchedule], previous_run: str | None, changes: list[Change],
    explanation: str,
) -> str:
    as_of = facts.as_of
    due = sum(schedule.deadline <= as_of for schedule in schedules)
    soon = sum(
        any(as_of < batch.deadline <= as_of + timedelta(days=30) for batch in schedule.batches)
        for schedule in schedules
    )
    batches = [batch for schedule in schedules for batch in schedule.batches]
    units_now = sum(batch.quantity for batch in batches if batch.deadline <= as_of)
    seasonal_units = sum(
        batch.quantity for batch in batches if batch.seasonal and as_of <= batch.deadline <= as_of + timedelta(days=90)
    )
    excess = excess_rows(facts)
    volume = round(fsum(max(0, row.stock_at_calculation + row.in_transit - (row.excess_threshold or 0)) for row in excess))
    metrics = (
        ("Поставщиков пора заказывать", due),
        ("Поставщиков в следующие 30 дней", soon),
        ("К заказу сейчас, шт.", units_now),
        ("Сезонные партии с крайним сроком в ближайшие 90 дней, шт.", seasonal_units),
        ("SKU в риске дефицита", len(stockout_rows(facts))),
        ("Избыток, шт. сверх порога", volume),
    )
    cards = "".join(f"<div><dt>{label}</dt><dd>{value}</dd></div>" for label, value in metrics)
    comparison = escape(" ".join(summarize(previous_run, changes).splitlines()))
    llm = f"<p><strong>Пояснение LLM</strong> · {escape(explanation)}</p>" if explanation else ""
    return f'<dl class="decision">{cards}</dl><p>{comparison}</p>{llm}'


def _risks(facts: PlanFacts) -> str:
    risks = stockout_rows(facts)
    visible = [row for row in risks if row.lost_sales >= 1]
    hidden = len(risks) - len(visible)
    hidden_label = "незначительный риск" if hidden % 10 == 1 and hidden % 100 != 11 else "незначительных"
    return _table(
        "Риски дефицита",
        ("SKU", "Поставщик", "Остаток, шт.", "В пути, шт.", "Хватит до", "Приход при заказе сегодня",
         "Дней без товара", "Упущенные продажи, шт."),
        ((row.sku_id, row.supplier_id, round(row.stock_at_calculation), round(row.in_transit),
          _date(row.stock_until), _date(row.arrival_date), row.stockout_days, round(row.lost_sales))
         for row in visible),
    ) + (f'<p class="meta">ещё {hidden} {hidden_label}</p>' if hidden else "")


def _excess(facts: PlanFacts) -> str:
    return _table(
        "Избыток",
        ("SKU", "Поставщик", "Остаток, шт.", "В пути, шт.", "Покрытие, мес", "Дециль цены", "Порог, шт.", "Флаг"),
        ((row.sku_id, row.supplier_id, round(row.stock_at_calculation), round(row.in_transit),
          coverage_text(row), row.price_decile if row.price_decile is not None else "—",
          round(row.excess_threshold) if row.excess_threshold is not None else "—",
          OrderFlag.RESCHEDULE if OrderFlag.RESCHEDULE in row.order_flags else "—")
         for row in excess_rows(facts)),
    )


def _seasons(facts: PlanFacts) -> str:
    deadlines: Counter[tuple[date, str, str]] = Counter()
    for row in facts.recommendations:
        if season := row.seasonal_order:
            deadline = season.deadline if season.deadline >= facts.as_of else season.next_deadline
            deadlines[deadline, row.supplier_id, row.category] += 1
    return _table(
        "Сезонные заказы", ("Крайний срок", "Поставщик", "Категория", "SKU"),
        ((_date(day), supplier, category, count) for (day, supplier, category), count in sorted(deadlines.items())),
    )


def _calendar(as_of: date, schedules: list[SupplierSchedule]) -> str:
    labels = ("янв.", "февр.", "март", "апр.", "май", "июнь", "июль", "авг.", "сент.", "окт.", "нояб.", "дек.")
    first = as_of.year * 12 + as_of.month - 1
    counts: Counter[int] = Counter()
    for schedule in schedules:
        days = (max(as_of, batch.deadline) for batch in schedule.batches)
        counts.update({day.year * 12 + day.month - 1 for day in days})
    months = [divmod(month, 12) for month in range(first, first + 6)]
    values = [counts[year * 12 + month] for year, month in months]
    scale = 72 / max(1, *values)
    bars = []
    for index, ((year, month), count) in enumerate(zip(months, values, strict=True)):
        y = 20 + 24 * index
        bars.append(
            f'<text x="0" y="{y}">{labels[month]} {year}</text>'
            f'<rect x="103" y="{y - 11}" width="{count * scale:.1f}" height="13" />'
            f'<text x="{109 + count * scale:.1f}" y="{y}">{count}</text>'
        )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 225 156" role="img" aria-labelledby="calendar-title">'
        '<title id="calendar-title">Число поставщиков по месяцу крайнего срока заказа</title>'
        + "".join(bars) + "</svg>"
    )


def _segments(facts: PlanFacts) -> str:
    counts = Counter(row.segment for row in facts.recommendations)
    return "<ul>" + "".join(f"<li>{segment} — {counts[segment]} SKU</li>" for segment in Segment) + "</ul>"


def _order_flags(facts: PlanFacts) -> str:
    counts = Counter(flag for row in facts.recommendations for flag in row.order_flags)
    return "<ul>" + "".join(f"<li>{flag} — {counts[flag]} SKU</li>" for flag in OrderFlag) + "</ul>"


def _backtest_quality(facts: PlanFacts) -> str:
    result = facts.backtest
    if result is None:
        return f"<p>{escape(facts.backtest_warning or 'Результаты бэктеста отсутствуют.')}</p>"
    warning = f"<p>{escape(facts.backtest_warning)}</p>" if facts.backtest_warning else ""
    metric = next((row for row in result.metrics if row.breakdown == row.group == "Все"), None)
    if metric is None or metric.wape is None or metric.bias is None:
        return f"{warning}<p>Недостаточно данных для оценки качества прогноза.</p>"
    overview = (f"<p>WAPE {_decimal(metric.wape * 100)}% · Смещение {_decimal(metric.bias * 100)}% · "
                "WAPE показывает суммарную абсолютную ошибку, смещение — отклонение прогноза от факта.</p>")
    breakdown = _table(
        "Бэктест по группам",
        ("Разрез", "Группа", "Окон", "WAPE", "Смещение", "WAPE среднего за 12 мес", "Смещение среднего за 12 мес"),
        ((row.breakdown, row.group, row.windows, *(
            f"{_decimal(value * 100)}%" if value is not None else "—"
            for value in (row.wape, row.bias, row.baseline_wape, row.baseline_bias)
        )) for row in result.metrics),
    )
    return overview + warning + f"<details><summary>По категориям и типам спроса</summary>{breakdown}</details>"


def write_dashboard(
    facts: PlanFacts, path: Path, *, run_date: date, previous_run: str | None, changes: list[Change],
    explanations: Explanations | None = None,
) -> None:
    age = (run_date - facts.cutoff).days
    stale = age > STALE_EXPORT_DAYS
    status = "Черновик, не размещать" if stale else "Актуально"
    status_class = "draft" if stale else "current"
    warnings = "".join(
        f"<p>{escape(warning)}</p>"
        for warning in (facts.cutoff_warning, facts.supply_warning) if warning
    )
    inventory = "".join(f"<li>{name} — {count} SKU</li>" for name, count in inventory_summary(facts).items())
    schedules = _supplier_schedules(facts)
    sections = (
        ("Сводка решения", _decision(facts, schedules, previous_run, changes, explanations.summary if explanations else "")),
        ("Заказ по поставщикам", _order_table(facts, schedules, explanations.rows if explanations else {})),
        ("Риски дефицита", _risks(facts)),
        ("Избыток", _excess(facts)),
        ("Сезонные заказы", _seasons(facts)),
        ("Календарь заказов на 6 месяцев", _calendar(facts.as_of, schedules)),
        ("Качество прогноза", _backtest_quality(facts)),
        ("Запас", f"<ul>{inventory}</ul>"),
        ("Сегменты ассортимента", _segments(facts)),
        ("Флаги открытых заказов", _order_flags(facts)),
    )
    content = "\n".join(f"<section><h2>{title}</h2>{body}</section>" for title, body in sections)
    history_link = '<a href="history.html">История запусков</a>' if previous_run else ""
    path.write_text(
        f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>План закупок</title>
<style>
:root {{
    color-scheme: light dark;
    --paper: #f7f9fc;
    --ink: #162437;
    --muted: #455975;
    --line: #cdd8e5;
    --accent: #1857a4;
    --draft: #783400;
    --draft-bg: #ffebcf;
}}
@media (prefers-color-scheme: dark) {{
    :root {{
        --paper: #111925;
        --ink: #edf2fa;
        --muted: #b7c8dc;
        --line: #42536b;
        --accent: #a7ceff;
        --draft: #ffe0b4;
        --draft-bg: #4c2c14;
    }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--paper); color: var(--ink); font: 1rem/1.5 system-ui, sans-serif; }}
main {{ max-width: 54rem; margin: auto; padding: clamp(1rem, 4vw, 3rem); }}
header {{ border-top: .25rem solid var(--accent); padding-top: 1rem; }}
h1 {{ font-size: clamp(1.7rem, 5vw, 2.6rem); margin: 0; }}
h2 {{ font-size: 1.2rem; margin: 0 0 .5rem; }}
section {{ margin-top: 1.75rem; }}
p {{ margin: .5rem 0; }}
.meta {{ color: var(--muted); }}
.draft, .current {{ display: inline-block; padding: .25rem .5rem; font-weight: 700; }}
.draft {{ background: var(--draft-bg); color: var(--draft); }}
.current {{ color: var(--accent); padding-left: 0; }}
a {{ color: var(--accent); display: inline-flex; align-items: center; min-height: 44px; }}
.decision {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .75rem; }}
.decision div {{ border-top: 1px solid var(--line); padding: .5rem 0; }}
.decision dd {{ font-size: 1.4rem; font-weight: 700; }}
dd {{ margin: 0; }}
details {{ border-bottom: 1px solid var(--line); }}
summary {{ padding: .7rem .25rem; cursor: pointer; overflow-wrap: anywhere; }}
.batch {{ display: block; margin-left: 1rem; }}
.skus {{ padding: 0; list-style: none; }}
.skus li {{ border-top: 1px solid var(--line); padding: .6rem 0 .6rem 1rem; overflow-wrap: anywhere; }}
.skus dl {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 2fr); gap: .1rem .5rem; margin: .3rem 0; }}
.table-scroll {{ overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }}
th, td {{ padding: .45rem .5rem; border-bottom: 1px solid var(--line); text-align: left; min-width: 5rem; }}
svg {{ width: min(100%, 24rem); display: block; }}
svg text {{ fill: var(--ink); font-size: 11px; }}
svg rect {{ fill: var(--accent); }}
ul {{ margin: 0; padding-left: 1.25rem; }}
</style>
</head>
<body>
<main>
<header>
<h1>План закупок</h1>
<p class="meta">Срез <time datetime="{facts.cutoff.isoformat()}">{facts.cutoff:%d.%m.%Y}</time> · {days_text(age)} на дату запуска</p>
<p><strong class="{status_class}">{status}</strong></p>
<a href="report.xlsx" download="report.xlsx">Скачать отчёт XLSX</a>
{history_link}
{warnings}
</header>
{content}
</main>
</body>
</html>
""",
        encoding="utf-8",
    )
