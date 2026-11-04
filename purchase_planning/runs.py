import argparse
import json
import sys
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from purchase_planning.comparison import DRIVER_LABELS, Change, compare, comparison_evidence
from purchase_planning.export import ExportValidationError
from purchase_planning.facts import Segment
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


def run_plan(data_dir: str | Path, history_dir: str | Path) -> Path:
    history = Path(history_dir).resolve()
    history.mkdir(parents=True, exist_ok=True)
    runs = (path for path in history.iterdir() if path.is_dir() and path.name.isascii() and path.name.isdecimal())
    previous = max(runs, key=lambda path: int(path.name), default=None)
    old_facts = _read_json(previous / "facts.json") if previous else None
    old_rows = old_facts["recommendations"] if old_facts else []
    previous_segments = {row["sku_id"]: Segment(row["segment"]) for row in old_rows}

    started_at = datetime.now(timezone.utc)
    run_date = started_at.astimezone().date()
    parameters = run_parameters()
    calculation = calculate(data_dir, previous_segments=previous_segments)
    facts = calculation.facts
    evidence = comparison_evidence(calculation.sales, calculation.receipts, calculation.orders)
    facts_data = asdict(facts)
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
        _write_json(staging / "run.json", {"started_at": started_at.isoformat(), "run_date": run_date.isoformat()})
        write_report(facts, staging / "report.xlsx", run_date=run_date, parameters=parameters)
        staging.rename(result)
    return result


def summarize(previous: str | None, changes: list[Change]) -> str:
    if previous is None:
        return "Первый запуск, истории для сравнения нет."
    if not changes:
        return "Изменений нет."

    quantities = sum(row["order_qty"][0] != row["order_qty"][1] for row in changes)
    segments = sum(row["segment"][0] != row["segment"][1] for row in changes)
    new_risks = sum(not before and after for before, after in (row["stockout_risk"] for row in changes))
    resolved_risks = sum(before and not after for before, after in (row["stockout_risk"] for row in changes))
    counts = {label: sum(code in row["drivers"] for row in changes) for code, label in DRIVER_LABELS.items()}
    causes = ", ".join(f"{label} — {count}" for label, count in counts.items() if count)
    unconfirmed = sum(bool(row["flags"]) for row in changes)
    return (
        f"К заказу изменилось у {quantities} SKU, сегмент — у {segments}. "
        f"Новые риски — {new_risks}, ушедшие — {resolved_risks}.\n"
        f"Наблюдаемые причины — {causes or 'нет'}. Без подтверждённой причины — {unconfirmed}."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Рассчитать план закупок и сравнить с предыдущим запуском.")
    parser.add_argument("data_dir", type=Path, help="Каталог выгрузки ERP")
    parser.add_argument("--history-dir", type=Path, default=Path(".scratch/purchase-runs"), help="Каталог истории")
    args = parser.parse_args()
    try:
        result = run_plan(args.data_dir, args.history_dir)
    except (ExportValidationError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"Запуск прерван. {error}", file=sys.stderr)
        return 1
    diff = _read_json(result / "diff.json")
    print(f"Отчёт: {result / 'report.xlsx'}")
    print(summarize(diff["previous_run"], diff["changes"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
