import shutil
from collections import Counter
from datetime import date
from pathlib import Path

from purchase_planning.backtest import run_backtest
from purchase_planning.planner import calculate_export, plan
from purchase_planning.prepared_export import prepare_export


def test_one_export_snapshot_supports_independent_calculation_dates(tmp_path):
    directory = Path(__file__).parent / "fixtures" / "export"
    for source in directory.iterdir():
        shutil.copyfile(source, tmp_path / source.name)
    dates = (date(2026, 7, 31), date(2026, 8, 13))
    expected = {day: plan(tmp_path, day) for day in dates}
    export = prepare_export(tmp_path)
    # Once prepared, editing the source cannot mix two ERP exports in one backtest.
    for source in tmp_path.glob("*.csv"):
        source.write_text("changed during calculation", encoding="utf-8")
    (tmp_path / "export_meta.json").write_text('{"cutoff_date":"2027-01-01"}', encoding="utf-8")
    for day in (dates[1], dates[0], dates[1]):
        assert calculate_export(export, day).facts == expected[day]
    assert not calculate_export(export, dates[0]).facts.recommendations[0].in_transit


def test_backtest_reads_each_erp_file_once(monkeypatch, tmp_path):
    for source in (Path(__file__).parent / "fixtures" / "export").iterdir():
        shutil.copyfile(source, tmp_path / source.name)
    directory = tmp_path
    (directory / "export_meta.json").write_text('{"cutoff_date":"2026-10-01"}', encoding="utf-8")
    sales = directory / "sales_monthly.csv"
    sales.write_text(sales.read_text(encoding="utf-8").replace("880,0,1,30,1", "40,0,1,30,0"), encoding="utf-8")
    orders = directory / "purchase_orders.csv"
    header = orders.read_text(encoding="utf-8").splitlines()[0]
    orders.write_text(
        header + "\n"
        "PO-1,SKU-1,SUP-1,import,2026-01-01,,-20,completed,100,100,100,0\n"
        "PO-2,SKU-1,SUP-1,import,2026-01-31,,-20,completed,50,50,50,0\n",
        encoding="utf-8",
    )
    original_open = Path.open
    reads = Counter()

    def counted_open(path, *args, **kwargs):
        if path.suffix == ".csv":
            reads[path.name] += 1
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)
    result = run_backtest(directory)
    assert result.origins == (date(2026, 6, 30), date(2026, 7, 31))
    assert reads == {path.name: 1 for path in directory.glob("*.csv")}
