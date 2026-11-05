"""Checks over the portal and XLSX built from the full anonymized export in the repo root.

Each check catches a class of mistake a buyer found by hand: dead links, developer jargon,
an overview number that does not match the list it opens.
"""

import json
import re
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

import pytest
from openpyxl import load_workbook

from purchase_planning.runs import run_plan
from purchase_planning.dashboard import write_dashboard
from purchase_planning.report import write_report
from purchase_planning.facts import PlanFacts, Recommendation, SeasonalOrder, SeasonStatus

ROOT = Path(__file__).parents[1]
METHODOLOGY = ROOT / "purchase_planning" / "methodology.md"
# Words a buyer without development context does not understand. See CODING_STANDARDS.md.
BANNED_WORDS = ("срез", "черновик", "дата запуска")
SELF_EXPLANATORY_COLUMNS = {"SKU", "Поставщик", "Почему", "Что сделать"}
ORDER_DATE_COLUMNS = ("Заказать не позже", "Дата заказа у поставщика", "Размещение обычной партии")
REPORT_DAY = date(2026, 11, 5)


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    with patch("purchase_planning.runs.datetime") as clock:
        clock.now.return_value = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
        return run_plan(ROOT, tmp_path_factory.mktemp("history"), stub_llm=True)


@pytest.fixture(scope="module")
def page(run_dir: Path) -> ElementTree.Element:
    return ElementTree.fromstring((run_dir / "index.html").read_text(encoding="utf-8").split("\n", 1)[1])


def _visible_text(node: ElementTree.Element) -> str:
    if node.tag in ("script", "style"):
        return ""
    return (node.text or "") + "".join(_visible_text(child) + (child.tail or "") for child in node)


def test_every_link_opens_something(page: ElementTree.Element, run_dir: Path) -> None:
    ids = {node.get("id") for node in page.iter() if node.get("id")}
    hrefs = {link.get("href", "") for link in page.iter("a")}
    assert {"#orders", "#decide", "#skip", "#risks", "#seasons", "report.xlsx"} <= hrefs
    assert sorted(href for href in hrefs if href.startswith("#") and href[1:] not in ids) == []
    local = {href for href in hrefs if href and not href.startswith(("#", "http://", "https://"))}
    assert sorted(href for href in local if not (run_dir / href).exists()) == []


def test_buyer_sees_no_developer_jargon(page: ElementTree.Element, run_dir: Path) -> None:
    texts = {
        "index.html": _visible_text(page),
        "methodology.md": METHODOLOGY.read_text(encoding="utf-8"),
    }
    workbook = load_workbook(run_dir / "report.xlsx", read_only=True)
    try:
        for sheet in workbook:
            texts[f"report.xlsx/{sheet.title}"] = "\n".join(
                value for row in sheet.iter_rows(values_only=True) for value in row if isinstance(value, str)
            )
    finally:
        workbook.close()
    found = sorted(
        (source, word) for source, text in texts.items() for word in BANNED_WORDS if word in text.lower()
    )
    assert found == []


def test_every_unclear_column_has_a_hint(page: ElementTree.Element) -> None:
    # The methodology tab is itself the explanation; its tables need no hover hints.
    method = page.find(".//section[@id='method']")
    explained = set() if method is None else set(method.iter())
    assert len(list(page.iter("th"))) > 20
    missing = sorted({
        header.text or "" for header in page.iter("th")
        if header not in explained and header.text not in SELF_EXPLANATORY_COLUMNS and not header.get("title")
    })
    assert missing == []


def _dates(value: object) -> list[date]:
    if isinstance(value, datetime):
        return [value.date()]
    if isinstance(value, date):
        return [value]
    if not isinstance(value, str):
        return []
    found = [date(int(y), int(m), int(d)) for d, m, y in re.findall(r"(\d\d)\.(\d\d)\.(\d{4})", value)]
    return found + [date.fromisoformat(iso) for iso in re.findall(r"\d{4}-\d\d-\d\d", value)]


def _assert_order_dates(page, workbook, today):
    orders = page.find(".//section[@id='orders']")
    assert orders is not None
    deadlines = [
        dd.text or "" for dl in orders.iter("dl")
        for dt, dd in zip(list(dl)[::2], list(dl)[1::2], strict=True) if dt.text == "Заказать не позже"
    ]
    assert deadlines, "Исчезли сроки заказа в карточках SKU"
    placements = [
        span.text or "" for span in orders.iter("span")
        if (span.text or "").startswith("Когда заказать —") or span.get("class") == "batch"
    ]
    assert placements, "Исчезли сроки поставщиков и партий"
    values = deadlines + placements
    assert any("Сейчас" in value for value in deadlines), "Пустые наступившие сроки HTML"
    assert any(_dates(value) for value in deadlines), "Пустые будущие сроки HTML"
    assert any("Сейчас" in value for value in placements), "Пустые сроки партий HTML"
    assert any(_dates(value) for value in placements), "Пустые будущие партии HTML"
    for name in ("Заказ по поставщикам", "Решить закупщику", "Не заказывать", "Прогноз"):
        rows = list(workbook[name].values)
        header_index = next(i for i, row in enumerate(rows) if "SKU" in row)
        header = rows[header_index]
        required = {"Заказать не позже"} if name == "Прогноз" else set(ORDER_DATE_COLUMNS)
        assert required <= set(header), (name, required - set(header))
        sheet_values = [row[header.index(col)] for row in rows[header_index + 1:] for col in required]
        if name in {"Заказ по поставщикам", "Прогноз"}:
            assert any("Сейчас" in str(value) for value in sheet_values), (name, "пустые наступившие сроки")
            assert any(_dates(value) for value in sheet_values), (name, "пустые будущие сроки")
        values.extend(sheet_values)
    assert any("Сейчас" in str(value) for value in values), "Не проверены наступившие сроки"
    assert any(_dates(value) for value in values), "Не проверены будущие сроки"
    assert [(value, d) for value in values for d in _dates(value) if d < today] == []


def test_no_order_date_in_the_past(page: ElementTree.Element, run_dir: Path) -> None:
    """Historical season deadlines and stock dates remain facts, not placement instructions."""
    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["run_date"] == REPORT_DAY.isoformat()
    workbook = load_workbook(run_dir / "report.xlsx", read_only=True)
    try:
        _assert_order_dates(page, workbook, REPORT_DAY)
    finally:
        workbook.close()


@pytest.mark.parametrize("tab", ["decide", "skip", "risks", "excess", "seasons"])
def test_overview_number_matches_its_list(page: ElementTree.Element, tab: str) -> None:
    overview = page.find(".//section[@id='overview']")
    assert overview is not None
    links = [link for link in overview.iter("a") if link.get("href") == f"#{tab}"]
    assert len(links) == 1
    section = page.find(f".//section[@id='{tab}']")
    assert section is not None
    if tab == "seasons":
        rows = section.findall(".//li[@data-season-soon='true']")
    else:
        rows = section.findall(".//tbody/tr")
    assert int((links[0].text or "").replace(" ", "").replace(" ", "")) == len(rows)


def _sheet_rows(workbook, name):
    rows = list(workbook[name].values)
    index = next(i for i, row in enumerate(rows) if "SKU" in row)
    return [dict(zip(rows[index], row, strict=True)) for row in rows[index + 1:]]


def _assert_skus(actual, expected):
    assert Counter(list(actual)) == Counter(list(expected)), "Состав SKU отличается, есть пропуск, подмена или дубль"


def test_sku_membership_matches_saved_facts_and_workbook(page, run_dir):
    rows = json.loads((run_dir / "facts.json").read_text(encoding="utf-8"))["recommendations"]
    groups = {
        "decide": ("Решить закупщику", [r for r in rows if r["segment"] == "Условно к закупке"]),
        "skip": ("Не заказывать", [r for r in rows if r["segment"] in {"Мёртвые", "Спящие", "На пересмотр"}]),
        "risks": ("Риски дефицита", [r for r in rows if r["stockout_risk"]]),
        "excess": ("Избыток", [r for r in rows if r["excess"]]),
        "seasons": ("Сезонные заказы", [r for r in rows if r["seasonal_order"] is not None]),
    }
    workbook = load_workbook(run_dir / "report.xlsx", read_only=True)
    try:
        for tab, (sheet, expected) in groups.items():
            assert expected, f"Эталонная выгрузка не покрывает {tab}"
            section = page.find(f".//section[@id='{tab}']")
            assert section is not None
            nodes = section.findall(".//li[@data-season-soon]") if tab == "seasons" else section.findall(".//tbody/tr/td[1]")
            _assert_skus([n.text for n in nodes], [r["sku_id"] for r in expected])
            _assert_skus([r["SKU"] for r in _sheet_rows(workbook, sheet)], [r["sku_id"] for r in expected])
        _assert_skus([r["SKU"] for r in _sheet_rows(workbook, "Прогноз")], [r["sku_id"] for r in rows])
    finally:
        workbook.close()


def _portal_orders(page):
    result = {}
    for detail in page.findall(".//section[@id='orders']//details[@class='sku-detail']"):
        sku = detail.find("./summary/span/strong").text
        assert sku not in result, sku
        fields = detail.find("dl")
        result[sku] = {dt.text: dd.text for dt, dd in zip(list(fields)[::2], list(fields)[1::2], strict=True)}
    assert result
    return result


def test_order_quantities_stock_and_supplier_totals_match_facts(page, run_dir):
    facts = json.loads((run_dir / "facts.json").read_text(encoding="utf-8"))
    expected = {r["sku_id"]: r for r in facts["recommendations"] if r["order_qty"] > 0}
    portal = _portal_orders(page)
    _assert_skus(portal, expected)
    workbook = load_workbook(run_dir / "report.xlsx", read_only=True)
    try:
        rows = _sheet_rows(workbook, "Заказ по поставщикам")
        _assert_skus([r["SKU"] for r in rows if r["SKU"] != "Итого"], expected)
        totals = Counter()
        for row in rows:
            if row["SKU"] == "Итого":
                continue
            sku = row["SKU"]
            fact = expected[sku]
            assert int(portal[sku]["К заказу, шт."]) == row["К заказу"] == fact["rounded_order_qty"]
            assert int(portal[sku]["Остаток, шт."]) == row["Остаток на дату расчёта"] == round(fact["stock_at_calculation"])
            assert int(portal[sku]["В пути, шт."]) == round(sum(qty for _, qty in fact["in_transit_by_month"]))
            totals[row["Поставщик"]] += row["К заказу"]
        total_rows = [r for r in rows if r["SKU"] == "Итого"]
        _assert_skus([r["Поставщик"] for r in total_rows], totals)
        assert {r["Поставщик"]: r["К заказу"] for r in total_rows} == dict(totals)
        assert facts["as_of"] == "2026-08-13"  # Reporting in November must not advance ERP inventory.
        parameters = dict(workbook["Параметры"].values)
        assert parameters["Дата расчёта"] == facts["as_of"]
        assert parameters["Дата формирования отчёта"] == REPORT_DAY.isoformat()
    finally:
        workbook.close()


def test_all_seven_tiles_match_workbook_summary(page, run_dir):
    tiles = page.findall(".//section[@id='overview']//dl/div")
    workbook = load_workbook(run_dir / "report.xlsx", read_only=True)
    try:
        summary = dict(workbook["Сводка"].values)
        matched = []
        for tile in tiles:
            label, link = tile.find("dt"), tile.find("dd/a")
            if link is None:
                continue
            name = re.sub(r" по данным на \d{2}\.\d{2}\.\d{4}", "", "".join(label.itertext()))
            assert int(link.text.replace(" ", "").replace(" ", "")) == summary[name]
            matched.append(name)
        assert len(matched) == len(set(matched)) == 7
    finally:
        workbook.close()


def _assert_metric_comments(page, workbook):
    headers = {h.text: h.get("title") for h in page.findall(".//section[@id='diagnostics']//th")}
    assert {"WAPE", "Смещение"} <= headers.keys()
    sheet = workbook["Бэктест"]
    row = next(cells for cells in sheet if any(cell.value == "WAPE" for cell in cells))
    assert len(row) == 7
    for cell in row:
        assert cell.comment and cell.comment.text.strip(), f"Нет подсказки XLSX: {cell.value}"
        assert cell.comment.text == headers[cell.value], cell.value


def test_metric_hints_match_in_html_and_xlsx(page, run_dir):
    workbook = load_workbook(run_dir / "report.xlsx")
    try:
        _assert_metric_comments(page, workbook)
    finally:
        workbook.close()


@pytest.mark.parametrize("broken", [
    "past_date", "missing_dates", "empty_html_dates", "empty_xlsx_dates", "missing_column", "missing_comment",
])
def test_invariants_reject_broken_artifacts(page, run_dir, broken):
    page = deepcopy(page)
    workbook = load_workbook(run_dir / "report.xlsx")
    try:
        if broken == "missing_comment":
            cell = next(c for row in workbook["Бэктест"] for c in row if c.value == "WAPE")
            cell.comment = None
            with pytest.raises(AssertionError, match="Нет подсказки"):
                _assert_metric_comments(page, workbook)
        else:
            if broken == "empty_xlsx_dates":
                sheet = workbook["Прогноз"]
                index = next(c.column for c in sheet[1] if c.value == "Заказать не позже")
                for cells in sheet.iter_rows(min_row=2):
                    cells[index - 1].value = None
            elif broken == "missing_column":
                cell = next(c for row in workbook["Прогноз"] for c in row if c.value == "Заказать не позже")
                cell.value = "Исчезнувшая колонка"
            else:
                for dl in page.findall(".//section[@id='orders']//dl"):
                    for dt, dd in zip(list(dl)[::2], list(dl)[1::2], strict=True):
                        if dt.text == "Заказать не позже":
                            if broken == "past_date":
                                dd.text = "02.10.2026"
                            elif broken == "empty_html_dates":
                                dd.text = "—"
                            else:
                                dt.text = "Исчезнувшая подпись"
            with pytest.raises(AssertionError):
                _assert_order_dates(page, workbook, REPORT_DAY)
    finally:
        workbook.close()


@pytest.mark.parametrize("actual", [["SKU-A", "SKU-C"], ["SKU-A", "SKU-A"]])
def test_membership_check_rejects_same_count_with_wrong_skus(actual):
    with pytest.raises(AssertionError, match="Состав SKU"):
        _assert_skus(actual, ["SKU-A", "SKU-B"])


def test_report_day_boundaries_without_advancing_inventory(tmp_path):
    cutoff = date(2026, 8, 13)
    rows = []
    for offset in (-1, 0, 1, 30, 31):
        deadline = REPORT_DAY + timedelta(days=offset)
        rows.append(Recommendation(
            f"REG-{offset}", f"SUP-REG-{offset}", 10, order_by=deadline, regular_order_date=deadline,
            stock_at_calculation=7, in_transit_by_month=((date(2026, 9, 1), 3),),
        ))
    for offset in (-1, 0, 90, 91):
        deadline = REPORT_DAY + timedelta(days=offset)
        rows.append(Recommendation(
            f"SEASON-{offset}", f"SUP-SEASON-{offset}", 10, regular_order_qty=0,
            seasonal_order=SeasonalOrder(
                date(2027, 5, 1), date(2027, 7, 31), deadline, 0, 3, 10, 10, 0,
                deadline + timedelta(days=365), SeasonStatus.ON_TIME, 0, 10, 0,
            ),
        ))
    facts = PlanFacts(cutoff, cutoff, tuple(rows))
    before = asdict(facts)
    write_dashboard(facts, tmp_path / "index.html", run_date=REPORT_DAY, previous_run=None, changes=[])
    write_report(facts, tmp_path / "report.xlsx", run_date=REPORT_DAY)
    assert asdict(facts) == before
    page = ElementTree.fromstring((tmp_path / "index.html").read_text(encoding="utf-8").split("\n", 1)[1])
    overview = page.find(".//section[@id='overview']")
    counts = [int(a.text) for a in overview.findall(".//dd/a")]
    # Literal expectations: yesterday/today are due, days 1..30 soon, seasonal days 0..90 included.
    assert counts == [4, 0, 0, 0, 2, 2, 0]
    soon = [li.text for li in page.findall(".//li[@data-season-soon='true']")]
    _assert_skus(soon, ["SEASON-0", "SEASON-90"])
    workbook = load_workbook(tmp_path / "report.xlsx", read_only=True)
    try:
        summary = dict(workbook["Сводка"].values)
        assert summary["Пора заказывать, SKU"] == 4
        assert summary["Заказать в ближайшие 30 дней, SKU"] == 2
        assert summary["Заказать: позже чем через 30 дней, SKU"] == 3
        assert summary["Заказать: пора заказывать, шт."] == 40
        assert summary["Сезонный заказ в ближайшие 90 дней, SKU"] == 2
        seasonal = _sheet_rows(workbook, "Сезонные заказы")
        _assert_skus(
            [r["SKU"] for r in seasonal if r["Сезонный заказ в ближайшие 90 дней"] == "Да"], soon,
        )
        portal = _portal_orders(page)
        for row in _sheet_rows(workbook, "Заказ по поставщикам"):
            if str(row["SKU"]).startswith("REG-"):
                assert row["Остаток на дату расчёта"] == 7
                assert row["В пути по месяцам"] == "2026-09 — 3"
                assert portal[row["SKU"]]["Остаток, шт."] == "7"
                assert portal[row["SKU"]]["В пути, шт."] == "3"
                if row["SKU"] in {"REG--1", "REG-0"}:
                    assert str(row["Заказать не позже"]).startswith("Сейчас")
                    assert portal[row["SKU"]]["Заказать не позже"].startswith("Сейчас")
    finally:
        workbook.close()


def test_navigation_search_pagination_and_disclosure_in_browser(run_dir):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            print("Browser processes:", browser.new_browser_cdp_session().send("SystemInfo.getProcessInfo"))
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto((run_dir / "index.html").as_uri())
            nav = page.locator(".workspace-nav a")
            for index in range(nav.count()):
                link = nav.nth(index)
                target = link.get_attribute("href")
                link.click()
                expect(page.locator(target)).to_be_visible()
                expect(page.locator("[data-view]:visible")).to_have_count(1)
                expect(link).to_have_attribute("aria-current", "location")

            page.locator('.workspace-nav a[href="#decide"]').click()
            rows = page.locator("#decide tbody tr:visible")
            expect(rows).to_have_count(8)
            first = rows.first.locator("td").first.inner_text()
            page.locator("#decide [data-next]").click()
            assert rows.first.locator("td").first.inner_text() != first
            page.locator("#decide [data-prev]").click()
            assert rows.first.locator("td").first.inner_text() == first
            sku = first.splitlines()[0].strip()
            search = page.locator("#decide input")
            search.fill(sku)
            expect(rows).to_have_count(1)
            expect(rows.first.locator("td").first).to_contain_text(sku)
            search.fill("NO-SUCH-SKU-000")
            expect(rows).to_have_count(0)
            expect(page.locator("#decide .empty")).to_be_visible()
            search.fill("")
            expect(rows).to_have_count(8)

            page.locator('.workspace-nav a[href="#orders"]').click()
            supplier = page.locator("#orders [data-entries] > details:visible").first
            supplier.locator(":scope > summary").click()
            expect(supplier).to_have_attribute("open", "")
            detail = supplier.locator(".sku-detail:visible").first
            detail.locator(":scope > summary").click()
            expect(detail.locator("dl")).to_be_visible()
            detail.locator(":scope > summary").click()
            expect(detail.locator("dl")).not_to_be_visible()
            assert errors == []
            print("Browser processes before close:", browser.new_browser_cdp_session().send("SystemInfo.getProcessInfo"))
        finally:
            browser.close()
