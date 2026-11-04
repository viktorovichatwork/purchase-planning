from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape
from math import fsum
from pathlib import Path

from purchase_planning.comparison import Change, summarize
from purchase_planning.explanations import Explanations
from purchase_planning.export import REQUIRED_COLUMNS
from purchase_planning.facts import OrderFlag, PlanFacts, Recommendation, Segment
from purchase_planning.order_sheet import (
    coverage_text, displayed_order_qty, excess_rows, order_date_label, rounded_quantity, stock_below_reorder_point,
    stockout_rows, supplier_orders,
)
from purchase_planning.parameters import STALE_EXPORT_DAYS
from purchase_planning.replenishment import inventory_summary
from purchase_planning.sku_labels import category_name
from purchase_planning.wording import days_text


_REPOSITORY_URL = "https://github.com/viktorovichatwork/purchase-planning"
_INVENTORY_HINTS = {
    "Покрытие 6–12 мес": "По прогнозу остатка и ожидаемых поставок хватит от 6 до 12 месяцев.",
    "Избыток": "Остаток и товар в пути превышают порог запаса для этой позиции. "
               "Порог учитывает прогноз, график закупок и сезон, если он есть.",
    "Риски дефицита": "По прогнозу товар закончится раньше, чем придёт новый заказ, "
                      "если разместить его на дату расчёта.",
}
_SEGMENT_HINTS = {
    Segment.DEAD: "За последние 12 месяцев продаж не было, хотя товар был на складе.",
    Segment.SLEEPING: "Товар продавался раньше. За последние 12 месяцев продаж и остатков не было.",
    Segment.REVIEW: "Проверьте цену и себестоимость. Причина может быть в низкой наценке, "
                    "неизвестной себестоимости или незавершённом пересмотре.",
    Segment.CONDITIONAL: "Проверьте рекомендацию перед заказом. Подтверждение байера нужно, "
                         "если мало данных, наценка чуть ниже порога или сезонный заказ запоздал.",
    Segment.PURCHASE: "Позиция проходит отбор для закупки. Количество и дату заказа смотрите у поставщика.",
}
_FLAG_HINTS = {
    OrderFlag.STUCK: "Заказ давно просрочен, существенная часть не пришла. "
                     "Уточните у поставщика, будет ли поставка. Пока её не учитывают в пути.",
    OrderFlag.TAIL: "Основная часть давно просроченного заказа пришла. "
                    "Небольшой остаток не учитывают в пути. Проверьте, можно ли закрыть заказ.",
    OrderFlag.RESCHEDULE: "По SKU есть избыток и открытый заказ. "
                          "Согласуйте с поставщиком перенос или отмену, если это возможно.",
}


_WORKSPACE_SCRIPT = """
//<![CDATA[
const pageSize = 8;
const normalize = (text) => text.toLocaleLowerCase("ru").replaceAll("ё", "е");

function setupList(list) {
    const entries = list.querySelector("[data-entries]");
    const search = list.querySelector(":scope > .search");
    const pager = list.querySelector(":scope > .pager");
    const empty = list.querySelector(":scope > .empty");
    const status = pager.querySelector("[role='status']");
    const prev = pager.querySelector("[data-prev]");
    const next = pager.querySelector("[data-next]");
    const items = Array.from(entries.children, (node) => ({
        node,
        text: normalize(node.dataset.search),
        child: node.querySelector(":scope > [data-browse]"),
    }));
    for (const item of items) {
        if (item.child) item.child = setupList(item.child);
    }
    let matches = items;
    let offset = 0;

    function render() {
        for (const item of items) item.node.hidden = true;
        for (const item of matches.slice(offset, offset + pageSize)) item.node.hidden = false;
        const count = matches.length;
        empty.hidden = count > 0;
        const table = entries.closest(".table-scroll");
        if (table) table.hidden = !count;
        pager.hidden = count === 0 || (!search && items.length <= pageSize);
        status.textContent = `Показано ${offset + 1}–${Math.min(offset + pageSize, count)} из ${count}`;
        prev.setAttribute("aria-disabled", offset === 0);
        next.setAttribute("aria-disabled", offset + pageSize >= count);
    }

    function filter(words) {
        matches = items.filter((item) => {
            const remaining = words.filter((word) => !item.text.includes(word));
            return item.child ? item.child(remaining) : remaining.length === 0;
        });
        offset = 0;
        render();
        return matches.length > 0;
    }

    if (search) {
        search.hidden = false;
        const input = search.querySelector("input");
        input.addEventListener("input", () => filter(normalize(input.value).trim().split(/\\s+/).filter(Boolean)));
    }
    prev.addEventListener("click", () => {
        if (offset === 0) return;
        offset -= pageSize;
        render();
    });
    next.addEventListener("click", () => {
        if (offset + pageSize >= matches.length) return;
        offset += pageSize;
        render();
    });
    render();
    return filter;
}

for (const list of document.querySelectorAll("[data-view] > [data-browse]")) setupList(list);

for (const hint of document.querySelectorAll(".hint")) {
    const trigger = hint.querySelector("button");
    const reset = () => delete hint.dataset.dismissed;
    hint.addEventListener("pointerenter", () => {
        const focused = document.querySelector(".hint:focus-within");
        if (focused && focused !== hint) focused.dataset.dismissed = "";
        reset();
    });
    trigger.addEventListener("focus", reset);
    trigger.addEventListener("click", () => {
        reset();
        trigger.focus();
    });
}
document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    for (const hint of document.querySelectorAll(".hint:hover, .hint:focus-within")) {
        hint.dataset.dismissed = "";
    }
});

const cutoffPicker = document.getElementById("cutoff-picker");
const cutoffInput = cutoffPicker.querySelector("input");
const cutoffDownload = cutoffPicker.querySelector("a");
cutoffInput.addEventListener("input", () => {
    if (!cutoffInput.validity.valid) {
        cutoffDownload.removeAttribute("href");
        cutoffDownload.setAttribute("aria-disabled", "true");
        return;
    }
    const contents = JSON.stringify({ cutoff_date: cutoffInput.value }, null, 2) + "\\n";
    cutoffDownload.href = "data:application/json;charset=utf-8," + encodeURIComponent(contents);
    cutoffDownload.removeAttribute("aria-disabled");
});
cutoffPicker.hidden = false;

const views = Array.from(document.querySelectorAll("[data-view]"));
const nav = document.querySelector(".workspace-nav");
const links = Array.from(nav.querySelectorAll("a"));
const defaultView = document.querySelector("#risks [data-browse]") ? "risks" : "orders";

function showView() {
    const target = document.getElementById(location.hash.slice(1));
    const active = target?.closest("[data-view]")?.id || defaultView;
    for (const view of views) view.hidden = view.id !== active;
    for (const link of links) {
        if (link.hash === "#" + active) link.setAttribute("aria-current", "location");
        else link.removeAttribute("aria-current");
    }
    return target;
}

for (const link of links) {
    link.addEventListener("click", (event) => {
        if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
        event.preventDefault();
        if (location.hash !== link.hash) history.pushState(null, "", link.hash);
        showView();
    });
}
window.addEventListener("hashchange", () => showView()?.scrollIntoView());
showView()?.scrollIntoView();
//]]>
"""


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


def _sku_caption(sku: str, labels: Mapping[str, str]) -> str:
    label = labels.get(sku)
    return f'<small class="sku-label">{escape(label)}</small>' if label else ""


def _search_key(*parts: str) -> str:
    return escape(" ".join(parts), quote=True)


def _browse(body: str, search_label: str = "") -> str:
    search = (
        f'<label class="search" hidden="">{search_label}'
        '<input type="search" placeholder="Например, Обогреватели" autocomplete="off" /></label>'
        if search_label else ""
    )
    return (
        f'<div class="browse" data-browse="">{search}{body}'
        '<p class="empty meta" hidden="">Совпадений нет. Измените запрос или очистите поле поиска.</p>'
        '<div class="pager" hidden=""><span class="meta" role="status"></span>'
        '<div><button type="button" data-prev="">Назад</button>'
        '<button type="button" data-next="">Далее</button></div></div></div>'
    )


def _table(
    label: str, headers: tuple[str, ...], rows: Iterable[tuple[object, ...]], *,
    sku_labels: Mapping[str, str] | None = None,
    supplier_column: int = 1,
) -> str:
    def render_row(row: tuple[object, ...]) -> str:
        first, *rest = map(str, row)
        attributes, caption = "", ""
        if sku_labels is not None:
            caption = _sku_caption(first, sku_labels)
            attributes = f' data-search="{_search_key(first, str(row[supplier_column]), sku_labels.get(first, ""))}"'
        tail = "".join(f"<td>{escape(value)}</td>" for value in rest)
        return f"<tr{attributes}><td>{escape(first)}{caption}</td>{tail}</tr>"

    body = "".join(render_row(row) for row in rows)
    if not body:
        return "<p>Нет позиций.</p>"
    heading = "".join(f'<th scope="col">{header}</th>' for header in headers)
    table = (
        f'<div class="table-scroll" role="region" aria-label="{label}" tabindex="0">'
        f'<table><thead><tr>{heading}</tr></thead><tbody data-entries="">{body}</tbody></table></div>'
    )
    return _browse(table, "Поиск по SKU, категории, бренду или поставщику") if sku_labels is not None else table


def _sku(
    row: Recommendation, facts: PlanFacts, explanation: str, supplier_flags: tuple[str, ...],
    labels: Mapping[str, str],
) -> str:
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
    sku = escape(row.sku_id)
    search = _search_key(row.sku_id, labels.get(row.sku_id, ""))
    return (
        f'<li data-search="{search}"><details class="sku-detail"><summary>'
        f'<span class="sku-heading"><strong>{sku}</strong>'
        f'<span>{displayed_order_qty(row)} шт. к заказу</span></span>{_sku_caption(row.sku_id, labels)}'
        f'</summary><dl>{fields_html}</dl>{why}</details></li>'
    )


def _order_table(
    facts: PlanFacts, schedules: list[SupplierSchedule], explanations: dict[str, str], labels: Mapping[str, str],
) -> str:
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
        skus = "".join(_sku(row, facts, explanations.get(row.sku_id, ""), schedule.flags, labels) for row in schedule.lines)
        entries = _browse(f'<ul class="skus" data-entries="">{skus}</ul>')
        rows.append(
            f'<details data-search="{name}"><summary><strong>{name}</strong> <span>Когда заказать — {when}</span>'
            f'{"".join(batch_lines)}</summary>{entries}</details>'
        )
    if not rows:
        return "<p>Нет позиций с положительным количеством к заказу.</p>"
    entries = f'<div data-entries="">{"".join(rows)}</div>'
    return '<p class="meta">Раскройте поставщика, чтобы увидеть позиции и сроки.</p>' + _browse(
        entries, "Поиск по поставщику, SKU, категории или бренду",
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
        ("К заказу сейчас, шт.", units_now),
        ("SKU в риске дефицита", len(stockout_rows(facts))),
    )
    additional = (
        ("Поставщиков в следующие 30 дней", soon),
        ("Сезонные партии с крайним сроком в ближайшие 90 дней, шт.", seasonal_units),
        ("Избыток, шт. сверх порога", volume),
    )

    def metrics_list(values: tuple[tuple[str, int], ...]) -> str:
        items = "".join(
            f"<div><dt>{label}</dt><dd>{format(value, ',').replace(',', ' ')}</dd></div>"
            for label, value in values
        )
        return f'<dl class="decision">{items}</dl>'

    comparison = escape(" ".join(summarize(previous_run, changes).splitlines()))
    llm = f"<p><strong>Пояснение LLM</strong> · {escape(explanation)}</p>" if explanation else ""
    return (
        f'{metrics_list(metrics)}<p>{comparison}</p>{llm}'
        f'<details class="more-metrics"><summary>Другие показатели</summary>{metrics_list(additional)}</details>'
    )


def _risks(facts: PlanFacts, labels: Mapping[str, str]) -> str:
    risks = stockout_rows(facts)
    visible = [row for row in risks if row.lost_sales >= 1]
    hidden = len(risks) - len(visible)
    return _table(
        "Риски дефицита",
        ("SKU", "Упущенные продажи, шт.", "Дней без товара", "Поставщик", "Хватит до",
         "Приход при заказе сегодня", "Остаток, шт.", "В пути, шт."),
        ((row.sku_id, round(row.lost_sales), row.stockout_days, row.supplier_id, _date(row.stock_until),
          _date(row.arrival_date), round(row.stock_at_calculation), round(row.in_transit))
         for row in visible),
        sku_labels=labels, supplier_column=3,
    ) + (f'<p class="meta">В XLSX есть ещё {hidden} SKU с оценкой потерь менее 1 шт.</p>' if hidden else "")


def _excess(facts: PlanFacts, labels: Mapping[str, str]) -> str:
    return _table(
        "Избыток",
        ("SKU", "Поставщик", "Остаток, шт.", "В пути, шт.", "Покрытие, мес", "Дециль цены", "Порог, шт.", "Флаг"),
        ((row.sku_id, row.supplier_id, round(row.stock_at_calculation), round(row.in_transit),
          coverage_text(row), row.price_decile if row.price_decile is not None else "—",
          round(row.excess_threshold) if row.excess_threshold is not None else "—",
          OrderFlag.RESCHEDULE if OrderFlag.RESCHEDULE in row.order_flags else "—")
         for row in excess_rows(facts)),
        sku_labels=labels,
    )


def _seasons(facts: PlanFacts, labels: Mapping[str, str]) -> str:
    groups: dict[tuple[date, str, str], list[str]] = defaultdict(list)
    for row in facts.recommendations:
        if season := row.seasonal_order:
            deadline = season.deadline if season.deadline >= facts.as_of else season.next_deadline
            groups[deadline, row.supplier_id, row.category].append(row.sku_id)
    if not groups:
        return "<p>Нет позиций.</p>"
    sections = []
    for (deadline, supplier, category), skus in sorted(groups.items()):
        heading = " · ".join(escape(part) for part in (
            _date(deadline), supplier or "Поставщик не указан", category_name(category), f"{len(skus)} SKU",
        ) if part)
        items = "".join(
            f'<li data-search="{_search_key(sku, labels.get(sku, ""))}">{escape(sku)}{_sku_caption(sku, labels)}</li>'
            for sku in sorted(skus)
        )
        entries = _browse(f'<ul class="skus season-skus" data-entries="">{items}</ul>')
        search = _search_key(supplier, category_name(category))
        sections.append(f'<details data-search="{search}"><summary>{heading}</summary>{entries}</details>')
    return '<p class="meta">Раскройте группу, чтобы увидеть SKU.</p>' + _browse(
        f'<div data-entries="">{"".join(sections)}</div>', "Поиск по категории, поставщику, SKU или бренду",
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


def _hint(identifier: str, label: str, description: str) -> str:
    return (
        f'<span class="hint"><button type="button" aria-describedby="{identifier}">{escape(label)}</button>'
        f'<span id="{identifier}" class="hint-text" role="tooltip">{escape(description)}</span></span>'
    )


def _hint_list(prefix: str, entries: Iterable[tuple[str, str]]) -> str:
    items = "".join(
        f"<li>{_hint(f'{prefix}-{index}', label, description)}</li>"
        for index, (label, description) in enumerate(entries, start=1)
    )
    return f'<ul class="hint-list">{items}</ul>'


def _segments(facts: PlanFacts) -> str:
    counts = Counter(row.segment for row in facts.recommendations)
    return _hint_list("segment", ((f"{segment} — {counts[segment]} SKU", _SEGMENT_HINTS[segment]) for segment in Segment))


def _order_flags(facts: PlanFacts) -> str:
    counts = Counter(flag for row in facts.recommendations for flag in row.order_flags)
    return _hint_list("flag", ((f"{flag} — {counts[flag]} SKU", _FLAG_HINTS[flag]) for flag in OrderFlag))


def _backtest_quality(facts: PlanFacts) -> str:
    result = facts.backtest
    if result is None:
        return f"<p>{escape(facts.backtest_warning or 'Результаты бэктеста отсутствуют.')}</p>"
    warning = f"<p>{escape(facts.backtest_warning)}</p>" if facts.backtest_warning else ""
    metric = next((row for row in result.metrics if row.breakdown == row.group == "Все"), None)
    if metric is None or metric.wape is None or metric.bias is None:
        return f"{warning}<p>Недостаточно данных для оценки качества прогноза.</p>"
    overview = _hint_list("accuracy", (
        (
            f"WAPE {_decimal(metric.wape * 100)}%",
            "Для каждого окна берём разницу между прогнозом и продажами по модулю. "
            "Складываем ошибки, делим на сумму фактических продаж и умножаем на 100%. "
            "Например, 38% означает 38 штук абсолютной ошибки на 100 проданных.",
        ),
        (
            f"Смещение {_decimal(metric.bias * 100)}%",
            "Складываем разницы «прогноз минус фактические продажи», делим на сумму фактических продаж "
            "и умножаем на 100%. Например, −3,2% означает, что общий прогноз был на 3,2% ниже факта.",
        ),
    ))
    context = (
        '<p class="meta">Проверяем прогноз на прошлых периодах. Каждое окно охватывает плечо поставки '
        'и цикл заказа поставщика. В расчёт входят только окна с полными данными и без дефицитных месяцев.</p>'
    )
    breakdown = _table(
        "Бэктест по группам",
        ("Разрез", "Группа", "Окон", "WAPE", "Смещение", "WAPE среднего за 12 мес", "Смещение среднего за 12 мес"),
        ((row.breakdown, row.group, row.windows, *(
            f"{_decimal(value * 100)}%" if value is not None else "—"
            for value in (row.wape, row.bias, row.baseline_wape, row.baseline_bias)
        )) for row in result.metrics),
    )
    return context + overview + warning + f"<details><summary>По категориям и типам спроса</summary>{breakdown}</details>"


def write_dashboard(
    facts: PlanFacts, path: Path, *, run_date: date, previous_run: str | None, changes: list[Change],
    explanations: Explanations | None = None,
    sku_labels: Mapping[str, str] | None = None,
) -> None:
    age = (run_date - facts.cutoff).days
    stale = age > STALE_EXPORT_DAYS
    status = "Устаревшая выгрузка" if stale else "Актуально"
    status_class = "draft" if stale else "current"
    warnings = "".join(
        f"<p>{escape(warning)}</p>"
        for warning in (facts.cutoff_warning, facts.supply_warning) if warning
    )
    inventory = _hint_list(
        "inventory",
        ((f"{name} — {count} SKU", _INVENTORY_HINTS[name]) for name, count in inventory_summary(facts).items()),
    )
    schedules = _supplier_schedules(facts)
    labels = sku_labels or {}
    overview = _decision(facts, schedules, previous_run, changes, explanations.summary if explanations else "")
    workspace = (
        ("orders", "Заказ по поставщикам", _order_table(facts, schedules, explanations.rows if explanations else {}, labels)),
        ("risks", "Риски дефицита", _risks(facts, labels)),
        ("excess", "Избыток", _excess(facts, labels)),
        ("seasons", "Сезонные заказы", _seasons(facts, labels)),
    )
    diagnostics = (
        ("calendar", "Календарь заказов на 6 месяцев", _calendar(facts.as_of, schedules)),
        ("forecast-quality", "Качество прогноза", _backtest_quality(facts)),
        ("stock", "Запас", inventory),
        ("segments", "Сегменты ассортимента", _segments(facts)),
        ("order-flags", "Флаги открытых заказов", _order_flags(facts)),
    )
    nav = "".join(f'<a href="#{key}">{"Поставщики" if key == "orders" else title}</a>' for key, title, _ in workspace)
    content = "\n".join(
        f'<section id="{key}" data-view=""><h2>{title}</h2>{body}</section>' for key, title, body in workspace
    )
    diagnostic_html = "\n".join(
        f'<section id="{key}"><h3>{title}</h3>{body}</section>' for key, title, body in diagnostics
    )
    history_link = '<a href="history.html">История запусков</a>' if previous_run else ""
    upload_link = '<a class="upload-action" href="#upload">Обновить выгрузку ERP</a>'
    stale_note = (
        f'<span>Перед размещением заказа обновите данные.</span>{upload_link}' if stale else ""
    )
    csv_files = ", ".join(f"<code>{table}.csv</code>" for table in REQUIRED_COLUMNS)
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
    --surface: #ffffff;
    --subtle: #eef3f9;
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
        --surface: #172231;
        --subtle: #202e40;
        --ink: #edf2fa;
        --muted: #b7c8dc;
        --line: #42536b;
        --accent: #a7ceff;
        --draft: #ffe0b4;
        --draft-bg: #4c2c14;
    }}
}}
* {{ box-sizing: border-box; }}
[hidden] {{ display: none !important; }}
body {{ margin: 0; background: var(--paper); color: var(--ink); font: 1rem/1.5 system-ui, sans-serif; }}
main {{ max-width: 76rem; margin: auto; padding: clamp(1rem, 3vw, 2rem); }}
header {{ display: flex; align-items: start; justify-content: space-between; flex-wrap: wrap; gap: .75rem 2rem; }}
h1 {{ font-size: 2rem; letter-spacing: -.025em; line-height: 1.15; margin: 0; }}
h2 {{ font-size: 1.25rem; margin: 0 0 .5rem; }}
h3 {{ font-size: 1rem; margin: 1.25rem 0 .35rem; }}
section {{ margin-top: 1.5rem; }}
p {{ margin: .5rem 0; }}
p:not(.empty) {{ max-width: 75ch; }}
.meta {{ color: var(--muted); }}
.freshness {{ flex-basis: 100%; display: flex; align-items: center; flex-wrap: wrap; gap: .15rem .75rem; padding: .65rem .9rem; border-radius: .5rem; }}
.draft {{ background: var(--draft-bg); color: var(--draft); }}
.draft a {{ color: inherit; font-weight: 650; }}
.upload-action {{ display: inline-flex; align-items: center; min-height: 44px; }}
.freshness .upload-action {{
    padding: 0 .75rem; border: 1px solid currentColor; border-radius: .4rem; text-decoration: none;
}}
.freshness .upload-action:hover {{ text-decoration: underline; }}
.current {{ color: var(--accent); background: var(--subtle); }}
a {{ color: var(--accent); text-underline-offset: .2em; }}
.actions {{ display: flex; align-items: center; flex-wrap: wrap; gap: .25rem 1rem; }}
.actions a, .workspace-nav a {{ display: inline-flex; align-items: center; min-height: 44px; }}
.download {{ border-radius: .4rem; padding: 0 .9rem; background: var(--accent); color: var(--surface); font-weight: 650; text-decoration: none; }}
.download:hover {{ text-decoration: underline; }}
.decision {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0 1.25rem; margin: .5rem 0; }}
.decision div {{ border-top: 1px solid var(--line); padding: .5rem 0; display: flex; flex-direction: column-reverse; justify-content: start; }}
.decision dt {{ color: var(--muted); font-size: .875rem; }}
.decision dd {{ font-size: 1.45rem; font-weight: 700; line-height: 1.2; font-variant-numeric: tabular-nums; }}
.more-metrics {{ border-bottom: 0; }}
.more-metrics summary {{ padding: .5rem 0; color: var(--accent); font-size: .9rem; }}
dd {{ margin: 0; }}
.workspace {{ margin-top: 1.5rem; background: var(--surface); border: 1px solid var(--line); border-radius: .75rem; }}
.workspace-nav {{ display: flex; flex-wrap: wrap; gap: .25rem .5rem; padding: .5rem .75rem; border-bottom: 1px solid var(--line); }}
.workspace-nav a {{ padding: 0 .75rem; color: var(--ink); font-size: .9rem; font-weight: 650; text-decoration: none; border-radius: .4rem; }}
.workspace-nav a:hover, summary:hover {{ background: var(--subtle); }}
.workspace-nav a[aria-current] {{ color: var(--accent); background: var(--subtle); }}
.workspace > section {{ margin: 0; padding: clamp(.75rem, 2vw, 1.5rem); }}
details {{ border-bottom: 1px solid var(--line); }}
summary {{ padding: .7rem .25rem; cursor: pointer; overflow-wrap: anywhere; }}
.batch {{ display: block; margin-left: 1rem; }}
.skus {{ padding: 0; list-style: none; }}
.skus li {{ overflow-wrap: anywhere; }}
.sku-detail summary {{ padding-inline: .75rem; }}
.sku-heading {{ display: inline-flex; flex-wrap: wrap; gap: 0 .75rem; }}
.sku-heading span {{ color: var(--muted); font-variant-numeric: tabular-nums; }}
.sku-detail .sku-label {{ margin-left: 1rem; }}
.skus dl {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 2fr); gap: .1rem .5rem; margin: .3rem .75rem .75rem; }}
.sku-detail p {{ margin-inline: .75rem; }}
.season-skus {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(12rem, 100%), 1fr)); gap: .6rem 1rem; padding: .75rem; }}
.sku-label {{ display: block; color: var(--muted); font-size: .85rem; overflow-wrap: anywhere; }}
td:has(.sku-label) {{ min-width: 10rem; }}
.table-scroll {{ overflow-x: auto; scrollbar-color: var(--line) var(--surface); }}
table {{ width: 100%; border-spacing: 0; font-variant-numeric: tabular-nums; font-size: .9rem; }}
th, td {{ padding: .55rem .65rem; border-bottom: 1px solid var(--line); text-align: left; min-width: 5rem; }}
th {{ background: var(--subtle); }}
td {{ vertical-align: top; }}
.browse td:first-child {{ font-weight: 650; }}
.browse td:first-child .sku-label {{ font-weight: 400; }}
.search {{ display: grid; gap: .25rem; width: min(100%, 30rem); margin: .75rem 0; font-size: .875rem; font-weight: 650; }}
input, button {{ font: inherit; color: var(--ink); min-height: 44px; border: 1px solid var(--line); border-radius: .4rem; background: var(--surface); }}
input {{ padding: 0 .75rem; font-weight: 400; caret-color: var(--accent); }}
input::placeholder {{ color: var(--muted); opacity: 1; }}
button {{ padding: 0 .75rem; cursor: pointer; }}
button:hover {{ background: var(--subtle); }}
button[aria-disabled="true"] {{ color: var(--muted); background: var(--subtle); cursor: default; }}
.hint-list {{ list-style: none; padding: 0; }}
.hint {{ position: relative; display: inline-block; max-width: 100%; }}
.hint button {{
    padding: .35rem 0; min-height: 2rem; border: 0; background: none; text-align: left;
    text-decoration: underline dotted; text-underline-offset: .25em;
}}
.hint button:hover {{ background: none; color: var(--accent); }}
.hint-text {{
    visibility: hidden; position: absolute; z-index: 2; inset-inline-start: 0; top: 100%;
    width: min(26rem, calc(100vw - 4rem)); padding: .65rem .8rem; border-radius: .45rem;
    background: var(--ink); color: var(--surface); font-size: .875rem;
}}
.hint:not([data-dismissed]):is(:hover, :focus-within) .hint-text {{ visibility: visible; }}
#order-flags .hint-text {{ top: auto; bottom: 100%; }}
.pager, .pager div {{ display: flex; align-items: center; flex-wrap: wrap; gap: .5rem; }}
.pager {{ justify-content: space-between; margin: .75rem 0 .25rem; font-size: .875rem; }}
:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
::selection {{ background: var(--accent); color: var(--surface); }}
svg {{ width: min(100%, 24rem); display: block; }}
svg text {{ fill: var(--ink); font-size: 11px; }}
svg rect {{ fill: var(--accent); }}
ul {{ margin: 0; padding-left: 1.25rem; }}
.guide ol {{ max-width: 75ch; }}
.guide ol {{ margin: 0; padding-left: 1.25rem; }}
.guide li + li {{ margin-top: .4rem; }}
.guide li a {{ display: inline; }}
.upload-steps > li + li {{ margin-top: 1.15rem; }}
.upload-steps h3 {{ margin: 0 0 .35rem; }}
.cutoff-picker {{ display: flex; align-items: end; flex-wrap: wrap; gap: .6rem .75rem; margin: .75rem 0; }}
.cutoff-picker label {{ display: grid; gap: .25rem; font-size: .875rem; font-weight: 650; }}
.cutoff-picker a.download {{ display: inline-flex; align-items: center; min-height: 44px; }}
.cutoff-picker a[aria-disabled] {{ background: var(--subtle); color: var(--muted); }}
@media (max-width: 40rem) {{
    h1 {{ font-size: 1.65rem; }}
    .decision {{ gap: 0 .6rem; }}
    .decision dt {{ font-size: .8rem; }}
    .workspace-nav a {{ padding-inline: .5rem; font-size: .85rem; }}
    .browse td:first-child, .browse th:first-child {{ position: sticky; left: 0; }}
    .browse td:first-child {{ background: var(--surface); }}
    .browse td:has(.sku-label) {{ min-width: 8.75rem; max-width: 8.75rem; }}
}}
</style>
</head>
<body>
<main>
<header>
<div>
<h1>План закупок</h1>
<p class="meta">Срез <time datetime="{facts.cutoff.isoformat()}">{facts.cutoff:%d.%m.%Y}</time> · {days_text(age)} на дату запуска</p>
</div>
<nav class="actions" aria-label="Действия с отчётом">
<a class="download" href="report.xlsx" download="report.xlsx">Скачать XLSX</a>
{history_link}
{upload_link if not stale else ""}
<a href="#guide">Как работать с порталом</a>
</nav>
<div class="freshness {status_class}"><strong>{status}</strong>{stale_note}</div>
{warnings}
</header>
<section id="overview"><h2>Сводка решения</h2>{overview}</section>
<div class="workspace">
<nav class="workspace-nav" aria-label="Рабочие разделы">
{nav}
<a href="#diagnostics">Аналитика</a>
</nav>
{content}
<section id="diagnostics" data-view="">
<h2>Аналитика</h2>
<p class="meta">Наведите курсор на подчёркнутый показатель или выберите его, чтобы прочитать пояснение.</p>
{diagnostic_html}
</section>
<section class="guide" id="guide" data-view="">
<h2>Как работать с порталом</h2>
<h3>Проверить рекомендации</h3>
<ol>
<li>Проверьте дату среза. Если выгрузке больше {STALE_EXPORT_DAYS} дней на дату запуска, <a href="#upload">обновите выгрузку ERP</a> перед заказом.</li>
<li>В разделе <a href="#orders">«Заказ по поставщикам»</a> раскройте поставщика и сверьте количество, сроки и флаги по SKU. Позиции «Условно к закупке» требуют подтверждения байера.</li>
<li>Проверьте <a href="#risks">риски дефицита</a>, <a href="#excess">избыток</a> и <a href="#seasons">сроки сезонных заказов</a>. Скачайте XLSX для работы со списками и проверки прогноза.</li>
</ol>
<h3>Прочитать подпись SKU</h3>
<p>Под кодом SKU указаны категория, обезличенный бренд и ценовая группа внутри категории. «Цена 7/10» означает седьмой дециль. В XLSX эта подпись находится в колонке «Товар».</p>
</section>
<section class="guide" id="upload" data-view="">
<h2>Обновить выгрузку ERP</h2>
<ol class="upload-steps">
<li>
<h3>Подготовьте шесть CSV</h3>
<p>Возьмите свежую обезличенную выгрузку из ERP. Проверьте, что в ней есть {csv_files}.</p>
</li>
<li>
<h3>Укажите, по какой день есть данные</h3>
<p>Выберите последний день, который включён в выгрузку. Например, если данные в ней по 13 августа, укажите 13 августа, даже если выгрузили их позже.</p>
<p>Исходный набор содержит шесть CSV. Портал создаёт отдельный файл <code>export_meta.json</code> с выбранной датой.</p>
<div class="cutoff-picker" id="cutoff-picker" hidden="">
<label>Дата среза<input type="date" name="cutoff_date" required="" /></label>
<a class="download" download="export_meta.json" aria-disabled="true">Скачать export_meta.json</a>
</div>
<details>
<summary>Как создать файл вручную</summary>
<p>Сохраните в UTF-8 файл с именем <code>export_meta.json</code> и содержимым <code>{{"cutoff_date":"2026-08-13"}}</code>. Замените дату из примера на свою в формате <code>ГГГГ-ММ-ДД</code>.</p>
<p>Если файла даты вообще нет в папке выгрузки, расчёт берёт последний день последнего полного месяца продаж. Дату среза внутри неполного месяца можно указать в файле.</p>
</details>
</li>
<li>
<h3>Загрузите файлы в GitHub</h3>
<p>Откройте <a href="{_REPOSITORY_URL}/tree/main/inbox">папку inbox ветки main</a> и выберите <strong>Add file → Upload files</strong>. Добавьте шесть CSV и новый <code>export_meta.json</code> с прежними именами. Нажмите <strong>Commit changes</strong>, чтобы сохранить их вместе в <code>main</code>. Нужен доступ на запись.</p>
<p>В папке уже есть файл даты от предыдущей выгрузки. Замените его вместе с CSV, иначе расчёт возьмёт старую дату.</p>
</li>
<li>
<h3>Проверьте результат</h3>
<p>Откройте <a href="{_REPOSITORY_URL}/actions/workflows/publish.yml">страницу расчётов в GitHub Actions</a>. Когда новый запуск завершится успешно, обновите портал и сверьте дату среза.</p>
</li>
</ol>
<p><a href="#guide">Вернуться к инструкции по работе с порталом</a></p>
</section>
</div>
</main>
<script>{_WORKSPACE_SCRIPT}</script>
</body>
</html>
""",
        encoding="utf-8",
    )
