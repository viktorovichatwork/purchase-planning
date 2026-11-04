import argparse
import json
import sys
from dataclasses import asdict
from datetime import date, datetime, timezone
from hashlib import sha256
from html import escape
from os import getenv
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory

from purchase_planning.backtest import run_backtest
from purchase_planning.calibration import BACKTEST_FILENAME, stock_multiplier
from purchase_planning.comparison import compare, comparison_evidence, summarize
from purchase_planning.dashboard import write_dashboard
from purchase_planning.explanations import ExplanationStats, explain, gateway
from purchase_planning.export import REQUIRED_COLUMNS, ExportValidationError
from purchase_planning.facts import PlanFacts, Segment
from purchase_planning.order_sheet import displayed_order_qty
from purchase_planning.parameters import run_parameters
from purchase_planning.planner import calculate
from purchase_planning.report import write_report


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, default=date.isoformat, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _runs(history: Path) -> list[Path]:
    return sorted(
        (path for path in history.iterdir() if path.is_dir() and path.name.isascii() and path.name.isdecimal()),
        key=lambda path: int(path.name), reverse=True,
    )


def _input_hash(directory: Path) -> str:
    digest = sha256()
    for name in (*(f"{table}.csv" for table in REQUIRED_COLUMNS), "export_meta.json"):
        path = directory / name
        if path.is_file():
            digest.update(name.encode("utf-8") + b"\0" + sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _validate_plan(facts: PlanFacts) -> None:
    if len({row.sku_id for row in facts.recommendations}) != len(facts.recommendations):
        raise ValueError("Результат содержит повторяющиеся SKU.")
    for row in facts.recommendations:
        if row.order_qty < 0 or (row.order_qty > 0 and row.segment not in {Segment.PURCHASE, Segment.CONDITIONAL}):
            raise ValueError(f"Некорректное количество к заказу для {row.sku_id}.")


def _history_page(entries: list[tuple[str, str, str]], path: Path) -> None:
    links = "".join(
        f'<li><a href="{escape(href, quote=True)}">Запуск {escape(name)} · срез {escape(cutoff)}</a></li>'
        for href, name, cutoff in entries
    )
    path.write_text(
        '<!doctype html><html lang="ru"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark"><title>История запусков</title>'
        f'<h1>История запусков</h1><ul>{links}</ul></html>\n',
        encoding="utf-8",
    )


def _build_site(history: Path, site: Path) -> None:
    runs = _runs(history)
    site.mkdir(parents=True, exist_ok=False)
    entries = []
    for run in runs:
        target = site / run.name
        target.mkdir()
        for filename in ("index.html", "report.xlsx", "history.html"):
            if (run / filename).is_file():
                copyfile(run / filename, target / filename)
        entries.append((f"{run.name}/index.html", run.name, _read_json(run / "facts.json")["cutoff"]))
    for filename in ("index.html", "report.xlsx"):
        copyfile(runs[0] / filename, site / filename)
    _history_page(entries[1:], site / "history.html")


def run_plan(
    data_dir: str | Path, history_dir: str | Path, *, stub_llm: bool = False,
    refresh_backtest: bool = False, reject_duplicate: bool = False,
    workflow_run: str | None = None, source_revision: str | None = None, code_revision: str | None = None,
) -> Path:
    directory = Path(data_dir)
    history = Path(history_dir).resolve()
    history.mkdir(parents=True, exist_ok=True)
    runs = _runs(history)
    previous = runs[0] if runs else None
    input_hash = _input_hash(directory)
    if workflow_run or reject_duplicate:
        metadata = [(path, _read_json(path / "run.json")) for path in runs]
        for path, run in metadata:
            if workflow_run and run.get("workflow_run") == workflow_run:
                if path != previous:
                    raise ValueError("У этого запуска уже есть более свежий преемник.")
                return path
        if reject_duplicate and any(run.get("input_sha256") == input_hash for _, run in metadata):
            raise ExportValidationError(["Такая выгрузка уже была обработана. Для пересчёта запустите workflow вручную."])
    old_facts = _read_json(previous / "facts.json") if previous else None
    old_rows = old_facts["recommendations"] if old_facts else []
    previous_segments = {row["sku_id"]: Segment(row["segment"]) for row in old_rows}

    started_at = datetime.now(timezone.utc)
    run_date = started_at.astimezone().date()
    parameters = run_parameters()
    with TemporaryDirectory(prefix="purchase-backtest-") as temporary:
        backtest_dir = Path(temporary) if refresh_backtest else directory
        if refresh_backtest:
            _write_json(backtest_dir / BACKTEST_FILENAME, asdict(run_backtest(directory)))
        calculation = calculate(directory, previous_segments=previous_segments, backtest_dir=backtest_dir)
    facts = calculation.facts
    _validate_plan(facts)
    if facts.backtest is not None:
        for group in facts.backtest.quantiles:
            parameters[f"Множитель, {group.lower()} спрос"] = stock_multiplier(facts.backtest, group, False)
            parameters[f"Сезонный множитель, {group.lower()} спрос"] = stock_multiplier(facts.backtest, group, True)
    evidence = comparison_evidence(calculation.sales, calculation.receipts, calculation.orders)
    facts_data = asdict(facts)
    for saved, recommendation in zip(facts_data["recommendations"], facts.recommendations, strict=True):
        saved["rounded_order_qty"] = displayed_order_qty(recommendation)
    changes = compare(
        old_rows, list(facts_data["recommendations"]), _read_json(previous / "evidence.json"), evidence,
        parameters_changed=_read_json(previous / "parameters.json") != parameters,
    ) if previous else []
    sequence = int(previous.name) + 1 if previous else 1
    result = history / f"{sequence:06d}"
    # A failed write stays invisible to later runs. Concurrent runs cannot replace a published directory.
    with TemporaryDirectory(dir=history, prefix=".pending-") as pending:
        staging = Path(pending) / "run"
        staging.mkdir()
        _write_json(staging / "facts.json", facts_data)
        _write_json(staging / "parameters.json", parameters)
        _write_json(staging / "evidence.json", evidence)
        _write_json(staging / "diff.json", {"previous_run": previous.name if previous else None, "changes": changes})
        write_report(facts, staging / "report.xlsx", run_date=run_date, parameters=parameters)
        explanations, llm_stats = explain(facts, changes, gateway(stub=stub_llm))
        _write_json(staging / "run.json", {
            "started_at": started_at.isoformat(), "run_date": run_date.isoformat(),
            "input_sha256": input_hash, "source_revision": source_revision, "code_revision": code_revision,
            "workflow_run": workflow_run, "llm": asdict(llm_stats),
        })
        write_dashboard(
            facts, staging / "index.html", run_date=run_date,
            previous_run=previous.name if previous else None, changes=changes, explanations=explanations,
        )
        if previous and old_facts:
            _history_page(
                [(f"../{previous.name}/index.html", previous.name, old_facts["cutoff"])], staging / "history.html",
            )
        staging.rename(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Рассчитать план закупок и сравнить с предыдущим запуском.")
    parser.add_argument("data_dir", type=Path, help="Каталог выгрузки ERP")
    parser.add_argument("--history-dir", type=Path, default=Path(".scratch/purchase-runs"), help="Каталог истории")
    parser.add_argument("--site-dir", type=Path, help="Новый каталог для сборки Pages")
    parser.add_argument("--stub-llm", action="store_true", help="Отключить сетевой вызов LLM")
    parser.add_argument("--refresh-backtest", action="store_true", help="Пересчитать бэктест по текущей выгрузке")
    parser.add_argument("--reject-duplicate", action="store_true", help="Отклонять ранее обработанную выгрузку")
    args = parser.parse_args()
    try:
        if args.site_dir and args.site_dir.exists():
            raise FileExistsError(f"Каталог сайта уже существует: {args.site_dir}")
        result = run_plan(
            args.data_dir, args.history_dir, stub_llm=args.stub_llm,
            refresh_backtest=args.refresh_backtest, reject_duplicate=args.reject_duplicate,
            workflow_run=getenv("GITHUB_RUN_ID") if getenv("PURCHASE_CODE_REVISION") else None,
            source_revision=getenv("GITHUB_SHA"), code_revision=getenv("PURCHASE_CODE_REVISION"),
        )
        if args.site_dir:
            _build_site(result.parent, args.site_dir)
    except (ExportValidationError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"Запуск прерван. {error}", file=sys.stderr)
        return 1
    diff = _read_json(result / "diff.json")
    print(f"Отчёт: {result / 'report.xlsx'}")
    print(f"Страница: {result / 'index.html'}")
    print(summarize(diff["previous_run"], diff["changes"]))
    if llm_stats := _read_json(result / "run.json").get("llm"):
        print(ExplanationStats(**llm_stats).message())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
