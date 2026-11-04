from collections import Counter
from datetime import date
from html import escape
from pathlib import Path

from purchase_planning.calibration import METRIC_HEADERS
from purchase_planning.comparison import Change, summarize
from purchase_planning.facts import OrderFlag, PlanFacts, Segment
from purchase_planning.order_sheet import supplier_orders
from purchase_planning.parameters import STALE_EXPORT_DAYS


def _order_table(facts: PlanFacts) -> str:
    summaries = {supplier.supplier_id: supplier for supplier in facts.suppliers}
    rows = []
    for supplier, _, total in supplier_orders(facts):
        summary = summaries.get(supplier)
        if summary and summary.time_to_order:
            when = "Сейчас"
        elif summary and summary.next_order_date:
            when = f"{summary.next_order_date:%d.%m.%Y}"
        else:
            when = "Срок не рассчитан"
        name = escape(supplier or "Поставщик не указан")
        rows.append(f'<tr><th scope="row">{name}</th><td>{when}</td><td>{total}</td></tr>')
    if not rows:
        return "<p>Нет позиций с положительным количеством к заказу.</p>"
    return (
        '<table><thead><tr><th scope="col">Поставщик</th><th scope="col">Когда заказать</th>'
        '<th scope="col">К заказу, шт.</th></tr></thead><tbody>'
        + "\n".join(rows) + "</tbody></table>"
    )


def _segments(facts: PlanFacts) -> str:
    counts = Counter(row.segment for row in facts.recommendations)
    return "<ul>" + "".join(f"<li>{segment} — {counts[segment]} SKU</li>" for segment in Segment) + "</ul>"


def _order_flags(facts: PlanFacts) -> str:
    counts = Counter(flag for row in facts.recommendations for flag in row.order_flags)
    return "<ul>" + "".join(f"<li>{flag} — {counts[flag]} SKU</li>" for flag in OrderFlag) + "</ul>"


def _backtest_table(facts: PlanFacts) -> str:
    result = facts.backtest
    if result is None:
        return f"<p>{escape(facts.backtest_warning or 'Результаты бэктеста отсутствуют.')}</p>"
    warning = f"<p>{escape(facts.backtest_warning)}</p>" if facts.backtest_warning else ""
    headers = "".join(f'<th scope="col">{label}</th>' for label in METRIC_HEADERS[1:])
    rows = []
    for row in result.metrics:
        percentages = (row.wape, row.bias, row.baseline_wape, row.baseline_bias)
        cells = "".join(f"<td>{value:.2%}</td>" if value is not None else "<td>—</td>" for value in percentages)
        rows.append(f'<tr><th scope="row">{escape(row.group)}</th><td>{row.windows}</td>{cells}</tr>')
    return (
        f"{warning}<p>Срез бэктеста {result.cutoff:%d.%m.%Y} · {len(result.origins)} точек отсчёта</p>"
        f"<table><thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def write_dashboard(
    facts: PlanFacts, path: Path, *, run_date: date, previous_run: str | None, changes: list[Change],
) -> None:
    age = (run_date - facts.cutoff).days
    stale = age > STALE_EXPORT_DAYS
    status = "Черновик, не размещать" if stale else "Актуально"
    status_class = "draft" if stale else "current"
    warnings = "".join(
        f"<p>{escape(warning)}</p>"
        for warning in (facts.cutoff_warning, facts.supply_warning) if warning
    )
    # Extend the dashboard by adding a section and its renderer here.
    sections = (
        ("Заказ по поставщикам", _order_table(facts)),
        ("Сегменты ассортимента", _segments(facts)),
        ("Флаги открытых заказов", _order_flags(facts)),
        ("Что изменилось с прошлого запуска", f"<p>{escape(summarize(previous_run, changes))}</p>"),
        ("Бэктест", _backtest_table(facts)),
    )
    content = "\n".join(f"<section><h2>{title}</h2>{body}</section>" for title, body in sections)
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
table {{ width: 100%; border-collapse: collapse; table-layout: fixed; font-variant-numeric: tabular-nums; }}
th, td {{ padding: .45rem .25rem; border-bottom: 1px solid var(--line); text-align: left; overflow-wrap: anywhere; }}
th:first-child {{ width: 38%; }}
th:last-child, td:last-child {{ width: 23%; text-align: right; }}
ul {{ margin: 0; padding-left: 1.25rem; }}
</style>
</head>
<body>
<main>
<header>
<h1>План закупок</h1>
<p class="meta">Срез <time datetime="{facts.cutoff.isoformat()}">{facts.cutoff:%d.%m.%Y}</time> · {age} дн. на дату запуска</p>
<p><strong class="{status_class}">{status}</strong></p>
<a href="report.xlsx" download="report.xlsx">Скачать отчёт XLSX</a>
{warnings}
</header>
{content}
</main>
</body>
</html>
""",
        encoding="utf-8",
    )
