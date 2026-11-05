from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import date, timedelta
from html import escape
from pathlib import Path

from purchase_planning import segments
from purchase_planning.actions import (
    ACTION_HINTS, Action, SkipReason, action, decision_points, skip_reason, todo_text, why_text,
)
from purchase_planning.calibration import METRIC_HEADERS, METRIC_HINTS
from purchase_planning.comparison import Change, summarize
from purchase_planning.explanations import Explanations
from purchase_planning.export import REQUIRED_COLUMNS
from purchase_planning.facts import OrderFlag, PlanFacts, Recommendation, SeasonStatus
from purchase_planning.methodology import methodology_html
from purchase_planning.order_sheet import (
    coverage_text, excess_rows, open_orders_text, order_date_label, stockout_rows,
)
from purchase_planning.orders import (
    OrderPlan, SupplierSchedule, displayed_order_qty, prepare_orders, rounded_quantity, stock_below_reorder_point,
)
from purchase_planning.parameters import STALE_EXPORT_DAYS, STALE_EXPORT_STATUS
from purchase_planning.replenishment import inventory_summary
from purchase_planning.sku_labels import category_name
from purchase_planning.summary import (
    ORDER_SOON_DAYS, SEASON_SOON_DAYS, PlanSummary, Tile, plan_summary, season_deadline,
    season_order_soon, summary_tiles,
)
from purchase_planning.wording import days_text, plural


_REPOSITORY_URL = "https://github.com/viktorovichatwork/purchase-planning"
# Upload form and progress polling (ADR 0007): the Make webhook commits the six CSV to inbox/ of main;
# the push starts the publish workflow, whose latest run the page polls through the public GitHub API.
_UPLOAD_URL = "https://hook.us1.make.com/s61yvcvbcciqgad773xol5np91hzgzk4"
# The export behind the published plan, packed by the publish workflow (absolute: archived run pages link to it too).
_EXPORT_ZIP_URL = "https://viktorovichatwork.github.io/purchase-planning/erp-export.zip"
_RUNS_API_URL = (
    "https://api.github.com/repos/viktorovichatwork/purchase-planning/actions/workflows/publish.yml/runs?per_page=1"
)
_INVENTORY_HINTS = {
    "Покрытие 6–12 мес": "По прогнозу остатка и ожидаемых поставок хватит от 6 до 12 месяцев.",
    "Избыток": "Остаток и товар в пути превышают порог запаса для этой позиции. "
               "Порог учитывает прогноз, график закупок и сезон, если он есть.",
    "Риски дефицита": "По прогнозу товар закончится раньше, чем придёт новый заказ, "
                      "если разместить его на дату расчёта.",
}
_COLUMN_HINTS = {
    "Остаток, шт.": "Сколько лежит на складе на дату данных.",
    "В пути, шт.": "Заказано у поставщика, плановая дата прихода в ERP ещё не наступила — сколько бы ни шла доставка.",
    "Заказы у поставщика": "Все открытые заказы по товару: сколько штук и когда придут. Если плановая дата прихода "
                           "уже прошла, а товар не пришёл, показано, на сколько дней заказ опаздывает. Такой заказ в "
                           "расчёт не берётся, поэтому система может предложить заказать товар снова.",
    "Покрытие, мес": "На сколько месяцев продаж хватит остатка и товара в пути по прогнозу.",
    "Ценовая группа": "Насколько дорогой товар внутри своей категории: 1 — самые дешёвые 10%, 10 — самые дорогие. "
                      "Чем выше группа, тем больше денег заморожено в избытке, поэтому такие товары стоят выше в списке.",
    "Порог избытка, шт.": "Сколько товара системе достаточно иметь. Берётся наибольшее из трёх: продажи на 12 месяцев; "
                          "для сезонных — продажи до конца ближайшего сезона; запас до прихода следующего заказа плюс партия. "
                          "Всё, что на складе и в пути сверх порога, — избыток.",
    "Открытый заказ": "«Перенести/отменить» — по товару уже есть избыток, а у поставщика ещё открыт заказ. "
                      "Его стоит перенести или отменить, если поставщик позволит.",
    "Упущенные продажи, шт.": "Сколько продаж не случится, пока товара нет, даже если заказать сегодня.",
    "Дней без товара": "Сколько дней товара не будет на складе до прихода заказа, размещённого сегодня.",
    "Хватит до": "День, когда по прогнозу закончится остаток вместе с товаром в пути.",
    "Приход при заказе сегодня": "Когда придёт заказ, если разместить его сегодня: сегодня плюс срок поставки поставщика.",
    "К заказу, шт.": "Количество, посчитанное системой, с округлением до обычной кратности заказа.",
    "Наценка за 12 мес, %": "Средняя наценка к себестоимости за 12 полных месяцев и текущий неполный, взвешенная по "
                            "проданным штукам. Так же её считает ERP в справочнике товаров. Пусто — себестоимость "
                            "не проставлена ни для одной продажи.",
    "Продано за 12 мес, шт.": "Сколько штук продано за 12 полных месяцев и текущий неполный месяц.",
    **METRIC_HINTS,
}
_FLAG_HINTS = {
    OrderFlag.STUCK: "Плановая дата прихода прошла, а товар не пришёл. Заказ в расчёт не берётся. "
                     "Если поставка всё же будет, обновите дату прихода в ERP — со следующей выгрузкой она снова "
                     "войдёт в «В пути».",
    OrderFlag.UNCONFIRMED: "В ERP у открытого заказа не заполнено подтверждённое количество. "
                           "Часто товар по таким заказам уже приходит: поле просто не заполняют. "
                           "Строки учитываются в пути по общим правилам.",
    OrderFlag.TAIL: "Плановая дата прихода прошла, основная часть заказа пришла. "
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
    const items = Array.from(entries.querySelectorAll(":scope > [data-search], :scope > section > ul > [data-search]"), (node) => ({
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

// Every list that is not inside another list; nested lists are set up by their parent.
for (const list of document.querySelectorAll("[data-browse]")) {
    if (!list.parentElement.closest("[data-browse]")) setupList(list);
}

// One explanation for every SKU caption, set here instead of repeating it in the HTML of each row.
for (const label of document.querySelectorAll(".sku-label")) {
    label.title = "Категория · обезличенный бренд · ценовая группа внутри категории: "
        + "1 из 10 — самые дешёвые 10% товаров категории, 10 из 10 — самые дорогие 10%.";
}

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

for (const link of [...links, ...document.querySelectorAll("[data-view-link]")]) {
    link.addEventListener("click", (event) => {
        if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
        event.preventDefault();
        if (location.hash !== link.hash) history.pushState(null, "", link.hash);
        const target = showView();
        if (link.hasAttribute("data-view-link") && target) {
            target.focus({preventScroll: true});
            target.scrollIntoView();
        }
    });
}
window.addEventListener("hashchange", () => showView()?.scrollIntoView());
showView()?.scrollIntoView();
//]]>
"""
_UPLOAD_SCRIPT = """
//<![CDATA[
const uploadForm = document.getElementById("upload-form");
const uploadStatus = document.getElementById("upload-status");
const uploadInput = uploadForm.querySelector("input[type=file]");
const uploadButton = uploadForm.querySelector("button");
const uploadList = document.getElementById("upload-files-list");
const uploadResult = document.getElementById("upload-result");
const tables = uploadForm.dataset.tables.split(" ");
const formatTime = (iso) => new Date(iso).toLocaleString("ru-RU", {dateStyle: "short", timeStyle: "short"});
const formatDay = (iso) => iso.split("-").reverse().join(".");
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function showStatus(text, kind) {
    uploadStatus.textContent = text;
    uploadStatus.dataset.kind = kind;
    uploadStatus.hidden = false;
}

// Every request has a deadline: a hung request must not stop the polling loop.
const fetchJson = (url) => fetch(url, {cache: "no-store", signal: AbortSignal.timeout(15000)})
    .then((response) => (response.ok ? response.json() : null))
    .catch(() => null);
// The same-origin status file decides when to reload: it changes only after the new site is deployed.
// The query string bypasses the Pages cache.
const publishedStatus = () => fetchJson(`status.json?t=${Date.now()}`);
// The public GitHub API only adds progress and failure messages; it may be rate-limited.
const latestRun = () => fetchJson(uploadForm.dataset.runsUrl).then((answer) => answer?.workflow_runs?.[0] ?? null);

let watching = false;
async function watchRecalculation(publishedAt, previousRunId) {
    if (watching) return;
    watching = true;
    const started = Date.now();
    while (Date.now() - started < 20 * 60 * 1000) {
        const status = await publishedStatus();
        if (status && status.at !== publishedAt) {
            location.replace(`${location.pathname}?updated=${encodeURIComponent(status.at)}${location.hash}`);
            return;
        }
        const run = await latestRun();
        if (run && run.id !== previousRunId) {
            if (run.status !== "completed") {
                showStatus("Идёт пересчёт по новой выгрузке. Страница обновится сама через 2–3 минуты.", "progress");
            } else if (run.conclusion !== "success") {
                showStatus("Пересчёт завершился ошибкой. На портале остаётся прежний план. "
                    + "Сообщите ответственному за систему.", "error");
                watching = false;
                return;
            }
        }
        await pause(10000);
    }
    showStatus("Пересчёт идёт дольше обычного. Обновите страницу позже.", "error");
    watching = false;
}

function chosenFiles() {
    const matched = new Map();
    const problems = [];
    for (const file of uploadInput.files) {
        const table = tables.find((name) => file.name.toLowerCase().includes(name));
        if (!table) problems.push(`${file.name} не похож ни на одну таблицу выгрузки`);
        else if (matched.has(table)) problems.push(`для ${table}.csv выбрано два файла`);
        else matched.set(table, file);
    }
    const missing = tables.filter((name) => !matched.has(name)).map((name) => `${name}.csv`);
    if (missing.length) problems.push(`не хватает: ${missing.join(", ")}`);
    return {matched, problems, missing};
}

const wholeExport = "Нужны все шесть файлов из одной выгрузки ERP. Таблицы связаны датой: если взять свежие продажи "
    + "и старые остатки, система посчитает, что на складе больше товара, чем есть, и предложит заказать меньше. "
    + "Выгрузите из ERP все шесть таблиц за один раз и загрузите их вместе.";

function selectionMessage({problems, missing}) {
    if (!problems.length) return "Все шесть файлов на месте.";
    return `Исправьте выбор: ${problems.join("; ")}.` + (missing.length ? ` ${wholeExport}` : "");
}

uploadInput.addEventListener("change", () => {
    const selection = chosenFiles();
    uploadList.replaceChildren(...tables.map((name) => {
        const item = document.createElement("li");
        item.textContent = `${name}.csv — ${selection.matched.has(name) ? selection.matched.get(name).name : "не выбран"}`;
        return item;
    }));
    uploadResult.textContent = selectionMessage(selection);
    uploadButton.disabled = selection.problems.length > 0;
});

uploadForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const selection = chosenFiles();
    const {matched, problems} = selection;
    if (problems.length) {
        uploadResult.textContent = selectionMessage(selection);
        return;
    }
    const body = new FormData();
    for (const [name, file] of matched) body.append(name, file, `${name}.csv`);
    uploadButton.disabled = true;
    uploadResult.textContent = "Загружаем файлы. Это займёт до минуты.";
    const [published, previousRun] = await Promise.all([publishedStatus(), latestRun()]);
    try {
        const response = await fetch(uploadForm.action, {method: "POST", body});
        const answer = await response.json().catch(() => ({}));
        if (response.ok && answer.changed === false) {
            uploadResult.textContent = "Эта выгрузка уже на портале: пересчитывать нечего.";
        } else if (response.ok && answer.ok) {
            uploadResult.textContent = "Файлы приняты. Идёт пересчёт, страница обновится сама через 2–3 минуты.";
            watchRecalculation(published?.at, previousRun?.id);
        } else if (answer.missing) {
            uploadResult.textContent = `Не хватает файлов: ${answer.missing.trim().split(" ").join(", ")}.`;
        } else {
            uploadResult.textContent = "Не удалось загрузить файлы. Попробуйте ещё раз через минуту.";
        }
    } catch {
        uploadResult.textContent = "Не удалось загрузить файлы. Проверьте соединение и попробуйте ещё раз.";
    } finally {
        uploadButton.disabled = chosenFiles().problems.length > 0;
    }
});

(async () => {
    const status = await publishedStatus();
    if (status && !status.accepted) {
        showStatus(`Загрузка ${formatTime(status.at)} отклонена: ${status.reasons.join(" ")} `
            + `На портале остаётся план по данным на ${formatDay(status.cutoff)}.`, "error");
    } else if (new URLSearchParams(location.search).has("updated")) {
        showStatus("Готово: план пересчитан по новой выгрузке.", "ok");
    }
    // A run that started before the published status is the one already shown here: its final checks
    // may still be running after the new site is deployed, so only a later run means a pending upload.
    const run = await latestRun();
    const pending = run && run.status !== "completed"
        && (!status || Date.parse(run.created_at) > Date.parse(status.at));
    if (pending) watchRecalculation(status?.at, null);
})();
//]]>
"""


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
    heading = "".join(
        f'<th scope="col" class="has-hint" title="{escape(_COLUMN_HINTS[header], quote=True)}">{header}</th>'
        if header in _COLUMN_HINTS else f'<th scope="col">{header}</th>'
        for header in headers
    )
    legend = "".join(
        f"<dt>{escape(header)}</dt><dd>{escape(_COLUMN_HINTS[header])}</dd>" for header in headers if header in _COLUMN_HINTS
    )
    # Hover shows the native tooltip. On touch screens the same text is under «Что значат столбцы».
    columns = f'<details class="columns"><summary>Что значат столбцы</summary><dl>{legend}</dl></details>' if legend else ""
    table = columns + (
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
        ("Заказы у поставщика", open_orders_text(row, facts.as_of)),
        ("Хватит до", _date(row.stock_until) if row.stock_until else (
            "Дольше горизонта" if row.coverage_months is not None else "Нет прогноза"
        )),
        ("Заказать не позже", order_date_label(
            row.order_by, facts.order_from, date_format="%d.%m.%Y", below_reorder_point=stock_below_reorder_point(row),
        ) if row.order_by else "—"),
        ("Покрытие, мес", coverage_text(row)),
        ("Что делать", action(row)),
        ("Флаги", ", ".join(dict.fromkeys((*row.order_flags, *row.sku_flags, *supplier_flags))) or "—"),
    )
    fields_html = "".join(f"<dt>{label}</dt><dd>{escape(str(value))}</dd>" for label, value in fields)
    why = f"<p><strong>Пояснение LLM</strong> · {escape(explanation)}</p>" if explanation else ""
    sku = escape(row.sku_id)
    search = _search_key(row.sku_id, labels.get(row.sku_id, ""))
    reason = "".join(
        f'<small class="confirmation-reason">{escape(why)} → {escape(todo)}</small>'
        for why, todo in decision_points(row)
    ) if action(row) == Action.DECIDE else ""
    return (
        f'<li data-search="{search}"><details class="sku-detail"><summary>'
        f'<span class="sku-heading"><strong>{sku}</strong>'
        f'<span>{displayed_order_qty(row)} шт. к заказу</span></span>{_sku_caption(row.sku_id, labels)}'
        f'{reason}</summary><dl>{fields_html}</dl>{why}</details></li>'
    )


def _order_intro(summary: PlanSummary) -> str:
    return (
        f'<p class="meta">В группе «{Action.ORDER}» {_number(summary.order_total)} SKU: '
        f"пора заказывать — {_number(summary.order_now)} SKU ({_number(summary.order_now_units)} шт. "
        f"у {_number(summary.order_now_suppliers)} "
        f"{plural(summary.order_now_suppliers, 'поставщика', 'поставщиков', 'поставщиков')}), "
        f"в ближайшие {days_text(ORDER_SOON_DAYS)} — "
        f"{_number(summary.order_soon)}, позже — {_number(summary.order_later)}. Ещё "
        f"{_number(summary.order_not_needed)} SKU заказывать не нужно: остатка и заказанного хватает, "
        f"в списке их нет. Товары, по которым нужно решение, стоят у поставщика отдельным блоком "
        f"«{Action.DECIDE}». Срок заказа «Сейчас» — у {_number(summary.due_suppliers)} "
        f"{plural(summary.due_suppliers, 'поставщика', 'поставщиков', 'поставщиков')}: у остальных "
        f"{_number(summary.due_suppliers - summary.order_now_suppliers)} к заказу сейчас только товары "
        f"«{Action.DECIDE}».</p>"
    )


def _order_table(
    facts: PlanFacts, schedules: tuple[SupplierSchedule, ...], explanations: dict[str, str], labels: Mapping[str, str],
    summary: PlanSummary,
) -> str:
    rows = []
    for schedule in schedules:
        as_of = facts.order_from
        when = order_date_label(schedule.deadline, as_of, date_format="%d.%m.%Y", below_reorder_point=schedule.low_stock)
        if not schedule.supplier_id:
            when = "после того, как закупщик назначит поставщика"
        name = escape(schedule.supplier_id or "Поставщик не указан")
        batch_lines = []
        for batch in schedule.placements:
            seasonal, deadline, quantity = batch.seasonal, batch.deadline, batch.quantity
            label = "Сезонная партия" if seasonal else "Обычная партия"
            batch_date = f"до {_date(deadline)}" if seasonal else _date(deadline)
            if deadline <= as_of:
                batch_date = "Сейчас"
            batch_lines.append(f'<span class="batch">{label} — {batch_date} — {quantity} шт.</span>')
        ordinary: list[str] = []
        conditional: list[str] = []
        for row in schedule.lines:
            target = conditional if action(row) == Action.DECIDE else ordinary
            target.append(_sku(row, facts, explanations.get(row.sku_id, ""), schedule.flags, labels))
        sku_groups = f'<section><ul class="skus">{"".join(ordinary)}</ul></section>' if ordinary else ""
        if conditional:
            sku_groups += (
                f'<section class="confirmation"><h3>{Action.DECIDE}</h3>'
                f'<ul class="skus">{"".join(conditional)}</ul></section>'
            )
        entries = _browse(f'<div class="sku-groups" data-entries="">{sku_groups}</div>')
        rows.append(
            f'<details data-search="{name}"><summary><strong>{name}</strong> <span>Когда заказать — {when}</span>'
            f'{"".join(batch_lines)}</summary>{entries}</details>'
        )
    if not rows:
        return _order_intro(summary) + "<p>Нет позиций с положительным количеством к заказу.</p>"
    entries = f'<div data-entries="">{"".join(rows)}</div>'
    return _order_intro(summary) + '<p class="meta">Раскройте поставщика, чтобы увидеть позиции и сроки.</p>' + _browse(
        entries, "Поиск по поставщику, SKU, категории или бренду",
    )


def _number(value: int) -> str:
    return format(value, ",").replace(",", " ")


def _decision(
    facts: PlanFacts, summary: PlanSummary, previous_run: str | None, changes: list[Change],
    explanation: str, *, stale: bool,
) -> str:
    def metrics_list(tiles: Iterable[Tile]) -> str:
        items = []
        for tile in tiles:
            label = tile.label
            if stale and tile.view == "orders" and tile.main:
                label = label.replace(", SKU", f" по данным на {facts.cutoff:%d.%m.%Y}, SKU")
            number = _number(tile.count)
            link = f'<a href="#{tile.view}" data-view-link="" aria-label="{escape(label)} — {number}">{number}</a>'
            items.append(f"<div><dt>{escape(label)}</dt><dd>{link}</dd></div>")
        return f'<dl class="decision">{"".join(items)}</dl>'

    tiles = summary_tiles(summary)
    comparison = escape(" ".join(summarize(previous_run, changes).splitlines()))
    llm = f"<p><strong>Пояснение LLM</strong> · {escape(explanation)}</p>" if explanation else ""
    return (
        f'{metrics_list(tile for tile in tiles if tile.main)}'
        '<p class="meta">Все показатели — в SKU. Нажмите на число, чтобы открыть вкладку с этими товарами.</p>'
        f'<p>{comparison}</p>{llm}'
        '<details class="more-metrics"><summary>Другие показатели</summary>'
        f'{metrics_list(tile for tile in tiles if not tile.main)}</details>'
    )


def _risks(facts: PlanFacts, labels: Mapping[str, str]) -> str:
    return _table(
        "Риски дефицита",
        ("SKU", "Упущенные продажи, шт.", "Дней без товара", "Поставщик", "Хватит до",
         "Приход при заказе сегодня", "Остаток, шт.", "В пути, шт.", "Заказы у поставщика"),
        ((row.sku_id, round(row.lost_sales) or "меньше 1", row.stockout_days,
          row.supplier_id, _date(row.stock_until), _date(row.arrival_date), round(row.stock_at_calculation),
          round(row.in_transit), open_orders_text(row, facts.as_of))
         for row in stockout_rows(facts)),
        sku_labels=labels, supplier_column=3,
    )


def _excess(facts: PlanFacts, labels: Mapping[str, str]) -> str:
    return _table(
        "Избыток",
        ("SKU", "Поставщик", "Остаток, шт.", "В пути, шт.", "Заказы у поставщика", "Покрытие, мес", "Ценовая группа",
         "Порог избытка, шт.", "Открытый заказ"),
        ((row.sku_id, row.supplier_id, round(row.stock_at_calculation), round(row.in_transit),
          open_orders_text(row, facts.as_of),
          coverage_text(row), row.price_decile if row.price_decile is not None else "—",
          round(row.excess_threshold) if row.excess_threshold is not None else "—",
          "Перенести/отменить" if OrderFlag.RESCHEDULE in row.order_flags else "—")
         for row in excess_rows(facts)),
        sku_labels=labels,
    )


def _seasons(facts: PlanFacts, labels: Mapping[str, str], summary: PlanSummary) -> str:
    groups: dict[tuple[date, str, str], list[Recommendation]] = defaultdict(list)
    for row in facts.recommendations:
        if deadline := season_deadline(row, facts.order_from):
            groups[deadline, row.supplier_id, row.category].append(row)
    if not groups:
        return "<p>Нет позиций.</p>"
    sections = []
    for (deadline, supplier, category), skus in sorted(groups.items()):
        historical = deadline < facts.order_from
        deadline_label = f"Исторический срок — {_date(deadline)}" if historical else _date(deadline)
        heading = " · ".join(escape(part) for part in (
            deadline_label, supplier or "Поставщик не указан", category_name(category), f"{len(skus)} SKU",
        ) if part)
        warning = (
            '<p class="meta">Этот срок уже прошёл. Количества ниже рассчитаны по старым данным. '
            'Для актуального сезонного плана загрузите свежую выгрузку ERP.</p>'
        ) if historical else ""
        items = []
        for row in sorted(skus, key=lambda row: row.sku_id):
            season = row.seasonal_order
            soon = season_order_soon(row, facts.order_from)
            if season and season.actionable:
                status = f"Сезонная партия — {rounded_quantity(season.order_qty, row.order_multiple)} шт."
                if soon:
                    status += f" · заказать в ближайшие {days_text(SEASON_SOON_DAYS)}"
                elif season.deadline < facts.order_from:
                    status += " · срок прошёл, решить закупщику"
            else:
                status = (
                    "Сезонный заказ не нужен: остатка и заказанного хватает"
                    if season and season.status == SeasonStatus.ON_TIME and season.order_qty <= 0
                    else str(season.status) if season else "Нет сезонного заказа"
                )
            items.append(
                f'<li data-season-soon="{str(soon).lower()}" data-search="{_search_key(row.sku_id, labels.get(row.sku_id, ""))}">'
                f'{escape(row.sku_id)}{_sku_caption(row.sku_id, labels)}<span class="batch">{escape(status)}</span></li>'
            )
        entries = _browse(f'<ul class="skus season-skus" data-entries="">{"".join(items)}</ul>')
        search = _search_key(supplier, category_name(category))
        sections.append(f'<details data-search="{search}"><summary>{heading}</summary>{warning}{entries}</details>')
    seasonal = sorted(
        (profile for profile in facts.category_profiles if profile.is_seasonal), key=lambda profile: profile.season_window,
    )
    windows = "; ".join(
        f"{category_name(profile.category).lower()} — {_MONTHS[profile.season_window[0] - 1]}–"
        f"{_MONTHS[profile.season_window[-1] - 1]}"
        for profile in seasonal
    )
    intro = (
        '<p class="meta">Сезонная категория — та, у которой за три лучших месяца подряд продаётся больше половины '
        f"годового объёма. Сейчас это: {escape(windows)}. Остальные категории продаются ровнее и заказываются обычным циклом.</p>"
        '<p class="meta">Под сезон заказ размещается один раз, заранее: крайний срок = начало сезона минус срок поставки '
        "поставщика. У разных поставщиков срок поставки разный, поэтому одна категория может стоять в нескольких группах — "
        "одна группа = один заказ одному поставщику. Если срок этого сезона уже прошёл, показан сохранённый срок "
        "следующего сезона. Если прошёл и он, группа помечена «Исторический срок»: для актуального плана нужны свежие данные. "
        "Раскройте группу, чтобы увидеть товары.</p>"
        f'<p class="meta">Всего в списке {_number(sum(map(len, groups.values())))} SKU. Нужен сезонный заказ в ближайшие '
        f"{days_text(SEASON_SOON_DAYS)}, до {_date(facts.order_from + timedelta(days=SEASON_SOON_DAYS))}, — у "
        f"{_number(summary.season_soon)} SKU. Эти товары отмечены в списке; товары без сезонной партии в показатель не входят.</p>"
    )
    return intro + _browse(
        f'<div data-entries="">{"".join(sections)}</div>', "Поиск по категории, поставщику, SKU или бренду",
    )


_MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь")


def _calendar(facts: PlanFacts, orders: OrderPlan) -> str:
    labels = ("янв.", "февр.", "март", "апр.", "май", "июнь", "июль", "авг.", "сент.", "окт.", "нояб.", "дек.")
    as_of = facts.order_from
    first = as_of.year * 12 + as_of.month - 1
    counts: Counter[int] = Counter()
    for row in facts.recommendations:
        days = (max(as_of, batch.deadline) for batch in orders.batches_by_sku[row.sku_id])
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
        '<title id="calendar-title">Число SKU по месяцу крайнего срока заказа</title>'
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


def _actions(facts: PlanFacts, summary: PlanSummary) -> str:
    counts = Counter(action(row) for row in facts.recommendations)
    groups = _hint_list("action", ((f"{group} — {counts[group]} SKU", ACTION_HINTS[group]) for group in Action))
    details = " · ".join(f"{reason} — {count}" for reason, count in summary.skip.items())
    return (
        f'{groups}<p class="meta">{Action.SKIP}: {escape(details)}. '
        f'Списки с причинами — во вкладке <a href="#skip" data-view-link="">«{Action.SKIP}»</a>.</p>'
    )


_SKIP_INTROS = {
    SkipReason.PRICE: "Наценка за последние 12 месяцев ниже {threshold}%: такой товар не заказывается автоматически. "
                      "Поднимите цену или выведите товар из ассортимента.",
    SkipReason.COST: "Себестоимость в ERP не проставлена для большинства продаж, поэтому наценку проверить нельзя. "
                     "Проставьте себестоимость — со следующей выгрузкой система проверит наценку.",
    SkipReason.NO_SALES: "За последние 12 месяцев продаж не было. Если товар есть на складе — распродайте или спишите "
                         "остаток. Если нет — решите, возвращать ли его в ассортимент.",
}


def _skip(facts: PlanFacts, labels: Mapping[str, str], summary: PlanSummary) -> str:
    lists = defaultdict(list)
    for row in facts.recommendations:
        if (reason := skip_reason(row)) is not None:
            lists[reason].append(row)
    intro = (
        f'<p class="meta">Система эти {_number(summary.skip_total)} SKU не заказывает. Они разделены на три списка '
        "по причине. У каждого товара написано, что сделать.</p>"
    )
    sections = []
    for reason in SkipReason:
        rows = lists[reason]
        if reason == SkipReason.NO_SALES:
            rows.sort(key=lambda row: (-row.stock_at_calculation, row.sku_id))
            headers: tuple[str, ...] = ("SKU", "Поставщик", "Остаток, шт.", "Почему", "Что сделать")
            values: Iterable[tuple[object, ...]] = (
                (row.sku_id, row.supplier_id or "Поставщик не указан", round(row.stock_at_calculation),
                 why_text(row), todo_text(row))
                for row in rows
            )
        else:
            rows.sort(key=lambda row: (-row.sales_qty_12m, row.sku_id))
            headers = ("SKU", "Поставщик", "Наценка за 12 мес, %", "Продано за 12 мес, шт.", "Почему", "Что сделать")
            values = (
                (row.sku_id, row.supplier_id or "Поставщик не указан", _decimal(row.markup_pct_12m),
                 round(row.sales_qty_12m), why_text(row), todo_text(row))
                for row in rows
            )
        description = _SKIP_INTROS[reason].format(threshold=segments.PURCHASE_MARKUP_PCT)
        sections.append(
            f'<section class="skip-list"><h3>{reason} — {_number(len(rows))} SKU</h3>'
            f'<p class="meta">{escape(description)}</p>{_table(reason, headers, values, sku_labels=labels)}</section>'
        )
    return intro + "".join(sections)


def _decide(facts: PlanFacts, labels: Mapping[str, str]) -> str:
    rows = sorted(
        (row for row in facts.recommendations if action(row) == Action.DECIDE),
        key=lambda row: (-displayed_order_qty(row), row.sku_id),
    )
    intro = (
        '<p class="meta">Количество посчитано, но заказ размещается только после решения закупщика. '
        "Если решение «заказать» — добавьте позицию в заказ поставщику.</p>"
    )
    return intro + _table(
        Action.DECIDE, ("SKU", "Поставщик", "К заказу, шт.", "Почему", "Что сделать"),
        ((row.sku_id, row.supplier_id or "Поставщик не указан", displayed_order_qty(row), why_text(row), todo_text(row))
         for row in rows),
        sku_labels=labels,
    )


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
        'и цикл заказа поставщика. Факт считаем по чистым продажам. '
        'В расчёт входят только окна с полными данными и без дефицитных месяцев.</p>'
    )
    breakdown = _table(
        "Бэктест по группам",
        METRIC_HEADERS,
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
    facts = replace(facts, today=run_date)
    age = (run_date - facts.cutoff).days
    stale = age > STALE_EXPORT_DAYS
    status = STALE_EXPORT_STATUS if stale else "Актуально"
    status_class = "draft" if stale else "current"
    warnings = f"<p>{escape(facts.supply_warning)}</p>" if facts.supply_warning else ""
    inventory = _hint_list(
        "inventory",
        ((f"{name} — {count} SKU", _INVENTORY_HINTS[name]) for name, count in inventory_summary(facts).items()),
    )
    orders = prepare_orders(facts)
    schedules = orders.suppliers
    labels = sku_labels or {}
    summary = plan_summary(facts, orders)
    overview = _decision(facts, summary, previous_run, changes, explanations.summary if explanations else "", stale=stale)
    order_table = _order_table(facts, schedules, explanations.rows if explanations else {}, labels, summary)
    workspace = (
        ("orders", "Заказ по поставщикам", order_table),
        ("decide", Action.DECIDE, _decide(facts, labels)),
        ("skip", Action.SKIP, _skip(facts, labels, summary)),
        ("risks", "Риски дефицита", _risks(facts, labels)),
        ("excess", "Избыток", _excess(facts, labels)),
        ("seasons", "Сезонные заказы", _seasons(facts, labels, summary)),
    )
    diagnostics = (
        ("calendar", "Календарь заказов на 6 месяцев, SKU", _calendar(facts, orders)),
        ("forecast-quality", "Качество прогноза", _backtest_quality(facts)),
        ("stock", "Запас", inventory),
        ("segments", "Что делать с ассортиментом", _actions(facts, summary)),
        ("order-flags", "Флаги открытых заказов", _order_flags(facts)),
    )
    nav = "".join(f'<a href="#{key}">{"Поставщики" if key == "orders" else title}</a>' for key, title, _ in workspace)
    content = "\n".join(
        f'<section id="{key}" data-view="" tabindex="-1"><h2>{title}</h2>{body}</section>' for key, title, body in workspace
    )
    diagnostic_html = "\n".join(
        f'<section id="{key}"><h3>{title}</h3>{body}</section>' for key, title, body in diagnostics
    )
    history_link = '<a href="history.html">История запусков</a>' if previous_run else ""
    upload_link = '<a class="upload-action" href="#upload">Обновить выгрузку ERP</a>'
    freshness = f'<div class="freshness {status_class}"><strong>{status}</strong>'
    if stale:
        freshness += (
            f'<span>Данным {days_text(age)}: остатки на складе уже другие. '
            'Перед заказом загрузите свежую выгрузку.</span>'
            '<a class="upload-action primary" href="#upload">Обновить выгрузку ERP</a>'
        )
    freshness += '</div>'
    check_orders = '<a class="primary" href="#orders" data-view-link="">Проверить заказ по поставщикам</a>'
    csv_files = ", ".join(f"<code>{table}.csv</code>" for table in REQUIRED_COLUMNS)
    tables = " ".join(REQUIRED_COLUMNS)
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
.current {{ color: var(--accent); background: var(--subtle); }}
a {{ color: var(--accent); text-underline-offset: .2em; }}
.actions {{ display: flex; align-items: center; flex-wrap: wrap; gap: .25rem 1rem; }}
.actions a, .workspace-nav a {{ display: inline-flex; align-items: center; min-height: 44px; }}
.primary {{ border-radius: .4rem; padding: 0 .9rem; background: var(--accent); color: var(--surface); font-weight: 650; text-decoration: none; }}
.draft .primary {{ background: var(--draft); color: var(--draft-bg); }}
.primary:hover {{ text-decoration: underline; }}
.decision {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(8rem, 100%), 1fr)); gap: 0 1.25rem; margin: .5rem 0; }}
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
.sku-groups section {{ margin: 0; }}
.sku-groups section:not(:has(li:not([hidden]))) {{ display: none; }}
.confirmation h3 {{ margin: .75rem .75rem 0; }}
.confirmation-reason {{ display: block; color: var(--muted); }}
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
th.has-hint {{ cursor: help; text-decoration: underline dotted; text-underline-offset: .25em; }}
.columns {{ margin: .5rem 0; font-size: .875rem; }}
.columns dl {{ display: grid; grid-template-columns: max-content 1fr; gap: .35rem 1rem; max-width: 75ch; }}
.columns dt {{ font-weight: 650; }}
.method {{ max-width: 80ch; }}
.method h3 {{ margin-top: 1.75rem; }}
.method blockquote {{ margin: .75rem 0; padding: .5rem 1rem; border-left: 3px solid var(--accent); background: var(--subtle); }}
.method li + li {{ margin-top: .3rem; }}
.columns dd {{ margin: 0; }}
@media (max-width: 40rem) {{ .columns dl {{ grid-template-columns: 1fr; }} .columns dd {{ margin-bottom: .4rem; }} }}
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
.upload-form {{ display: grid; gap: .5rem; justify-items: start; margin-top: .75rem; }}
.upload-form input[type=file] {{ padding: .5rem; max-width: 100%; }}
.upload-form button.primary {{ border: 0; }}
.upload-form button:disabled {{ opacity: .6; cursor: default; }}
.upload-status {{ flex-basis: 100%; margin: 0; padding: .65rem .9rem; border-radius: .5rem; max-width: none; }}
.upload-status[data-kind="error"] {{ background: var(--draft-bg); color: var(--draft); }}
.upload-status[data-kind="progress"], .upload-status[data-kind="ok"] {{ background: var(--subtle); color: var(--accent); }}
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
<p class="meta">Остатки и продажи из ERP на <time datetime="{facts.cutoff.isoformat()}">{facts.cutoff:%d.%m.%Y}</time> · пересчитано <time datetime="{run_date.isoformat()}">{run_date:%d.%m.%Y}</time></p>
</div>
{freshness if stale else ""}
<nav class="actions" aria-label="Действия с отчётом">
{check_orders if not stale else ""}
<a href="report.xlsx" download="report.xlsx">Скачать XLSX</a>
{history_link}
{upload_link if not stale else ""}
<a href="#guide">Как работать с порталом</a>
</nav>
{freshness if not stale else ""}
<p id="upload-status" class="upload-status" role="status" hidden=""></p>
{warnings}
</header>
<section id="overview"><h2>Сводка решения</h2>{overview}</section>
<div class="workspace">
<nav class="workspace-nav" aria-label="Рабочие разделы">
{nav}
<a href="#diagnostics">Аналитика</a>
<a href="#method">Методика</a>
</nav>
{content}
<section id="diagnostics" data-view="">
<h2>Аналитика</h2>
<p class="meta">Наведите курсор на подчёркнутый показатель или выберите его, чтобы прочитать пояснение.</p>
{diagnostic_html}
</section>
<section class="method" id="method" data-view="">
<h2>Как система считает закупку</h2>
{methodology_html()}
</section>
<section class="guide" id="guide" data-view="">
<h2>Как работать с порталом</h2>
<h3>Проверить рекомендации</h3>
<ol>
<li>Проверьте дату данных под заголовком. Если данным больше {STALE_EXPORT_DAYS} дней, <a href="#upload">загрузите свежую выгрузку ERP</a> перед заказом.</li>
<li>В разделе <a href="#orders">«Заказ по поставщикам»</a> раскройте поставщика и сверьте количество, сроки и флаги по SKU.</li>
<li>Откройте <a href="#decide">«Решить закупщику»</a>. У каждого товара написано, почему система не заказывает его сама и что сделать. Решили заказать — добавьте позицию в заказ поставщику.</li>
<li>Во вкладке <a href="#skip">«Не заказывать»</a> три списка: пересмотреть цену (наценка ниже {segments.PURCHASE_MARKUP_PCT}%), проставить себестоимость и год без продаж. Передайте их тем, кто отвечает за цены и ассортимент.</li>
<li>Проверьте <a href="#risks">риски дефицита</a>, <a href="#excess">избыток</a> и <a href="#seasons">сроки сезонных заказов</a>. Скачайте XLSX для работы со списками и проверки прогноза.</li>
</ol>
<h3>Как система считает</h3>
<p>Откуда берутся прогноз, страховой запас, количество и сроки, простыми словами — вкладка <a href="#method">«Методика»</a>.</p>
<h3>Прочитать подпись SKU</h3>
<p>Под кодом SKU указаны категория, обезличенный бренд и ценовая группа внутри категории. Все товары категории разбиты по цене на 10 равных групп: «ценовая группа 1 из 10» — самые дешёвые 10%, «10 из 10» — самые дорогие 10%. Названий товаров в выгрузке ERP нет, поэтому подпись помогает понять, что это за товар. В XLSX эта подпись — в колонке «Товар».</p>
</section>
<section class="guide" id="upload" data-view="">
<h2>Обновить выгрузку ERP</h2>
<ol class="upload-steps">
<li>
<h3>Подготовьте шесть CSV</h3>
<p>Возьмите свежую обезличенную выгрузку из ERP. Проверьте, что в ней есть {csv_files}.</p>
<p>Нет выгрузки под рукой? <a href="{_EXPORT_ZIP_URL}" download="">Скачайте выгрузку, по которой посчитан текущий план</a> (ZIP с шестью CSV). Её можно изменить и загрузить обратно, чтобы посмотреть, как меняется план.</p>
<p>Все шесть таблиц выгружайте за один раз и загружайте вместе. Таблицы связаны датой: свежие продажи со старыми остатками исказят заказ, поэтому заменить одну таблицу нельзя.</p>
</li>
<li>
<h3>Загрузите файлы здесь</h3>
<p>Выберите все шесть файлов сразу и нажмите «Загрузить и пересчитать». Имя файла может отличаться, главное — чтобы в нём было название таблицы, например «sales_monthly (1).csv». Загрузка заменяет все шесть таблиц и сохраняется в истории запусков.</p>
<form id="upload-form" class="upload-form" action="{_UPLOAD_URL}" method="post" enctype="multipart/form-data" data-tables="{tables}" data-runs-url="{escape(_RUNS_API_URL, quote=True)}">
<label class="search" for="upload-files">Шесть CSV из ERP
<input id="upload-files" type="file" accept=".csv,text/csv" multiple="" required="" /></label>
<ul id="upload-files-list" class="meta"></ul>
<button class="primary" type="submit" disabled="">Загрузить и пересчитать</button>
<p id="upload-result" class="meta" role="status"></p>
</form>
</li>
<li>
<h3>Дождитесь пересчёта</h3>
<p>Пересчёт занимает 2–3 минуты, страница обновится сама. Сверьте дату данных под заголовком. Если выгрузку отклонят, под заголовком появится причина, а на портале останется прежний план. Ход расчёта виден и на <a href="{_REPOSITORY_URL}/actions/workflows/publish.yml">странице расчётов в GitHub Actions</a>.</p>
<p>Даты в выгрузке нет. Портал определяет её сам: сравнивает продажи последнего, неполного месяца с обычными продажами за такой месяц. Например, продана примерно половина обычного — значит, данные на середину месяца.</p>
</li>
</ol>
<p><a href="#guide">Вернуться к инструкции по работе с порталом</a></p>
</section>
</div>
</main>
<script>{_WORKSPACE_SCRIPT}</script>
<script>{_UPLOAD_SCRIPT}</script>
</body>
</html>
""",
        encoding="utf-8",
    )
