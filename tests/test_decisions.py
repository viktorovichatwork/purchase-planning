from dataclasses import replace
from pathlib import Path

import pytest
from openpyxl import load_workbook

from purchase_planning import actions, plan, segments
from purchase_planning.actions import decision_points, skip_reason
from purchase_planning.facts import DecisionReason, Segment
from purchase_planning.report import write_report


@pytest.fixture(scope="module")
def facts():
    return plan(Path(__file__).resolve().parents[1])


def test_recorded_decisions_preserve_existing_explanations(facts):
    for row in facts.recommendations:
        assert row.decision is not None
        legacy = replace(row, decision=None)
        assert decision_points(row) == decision_points(legacy), row.sku_id
        assert skip_reason(row) == skip_reason(legacy), row.sku_id


def test_recorded_decisions_do_not_depend_on_flags_or_later_thresholds(facts, monkeypatch):
    expected = {row.sku_id: (decision_points(row), skip_reason(row)) for row in facts.recommendations}
    monkeypatch.setattr(segments, "PURCHASE_MARKUP_PCT", 99)
    monkeypatch.setattr(actions, "LONG_STOCKOUT_SHARE", 0)
    assert any(DecisionReason.LONG_STOCKOUT in row.decision.reasons for row in facts.recommendations)
    assert any(DecisionReason.TYPICAL_TIMING in row.decision.reasons for row in facts.recommendations)
    assert any(row.segment == Segment.REVIEW for row in facts.recommendations)
    for row in facts.recommendations:
        changed = replace(row, sku_flags=("Текст флага изменился",))
        assert (decision_points(changed), skip_reason(changed)) == expected[row.sku_id]


def test_workbook_uses_recorded_typical_timing_without_a_text_flag(facts, tmp_path):
    row = next(row for row in facts.recommendations if DecisionReason.TYPICAL_TIMING in row.decision.reasons)
    timing = row.decision.typical_timing
    assert timing is not None
    changed = replace(facts, recommendations=(replace(row, sku_flags=()),), typical_timing=None)
    path = tmp_path / "report.xlsx"
    write_report(changed, path, run_date=facts.cutoff)
    workbook = load_workbook(path)
    try:
        cells = list(workbook["Решить закупщику"].values)
        headers, values = cells[0], cells[1]
        assert values[headers.index("Плечо, дней")] == timing.lead_days
        assert values[headers.index("Цикл, дней")] == timing.cycle_days
        assert values[headers.index("Источник плеча")] == timing.lead_source
        assert f"{timing.lead_days} дн." in values[0]
    finally:
        workbook.close()
