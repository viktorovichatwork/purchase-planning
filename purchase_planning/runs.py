import argparse
import json
import sys
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from purchase_planning.comparison import compare, comparison_evidence, summarize
from purchase_planning.dashboard import write_dashboard
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
        write_dashboard(
            facts, staging / "index.html", run_date=run_date,
            previous_run=previous.name if previous else None, changes=changes,
        )
        staging.rename(result)
    return result


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
    print(f"Страница: {result / 'index.html'}")
    print(summarize(diff["previous_run"], diff["changes"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
