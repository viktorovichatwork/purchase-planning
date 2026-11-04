from datetime import date, timedelta

from purchase_planning.facts import (
    PlanFacts, Recommendation, SeasonalOrder, SeasonStatus, Segment, SupplierSummary, SupplierTiming,
)
from purchase_planning.orders import prepare_orders
from purchase_planning.summary import plan_summary


def test_batches_keep_their_own_rounding_dates_and_buyer_decisions():
    day = date(2026, 8, 13)
    season = SeasonalOrder(
        window_start=day, window_end=day + timedelta(days=90), deadline=day - timedelta(days=30),
        days_late=30, months_remaining=3, order_qty=5.1, ordinary_service_qty=6,
        stock_at_season_end=0, next_deadline=date(2027, 7, 14), status=SeasonStatus.LATE,
        stock_at_arrival=0, target_stock=6, safety_stock=0,
    )
    facts = PlanFacts(day, day, (
        Recommendation("mixed", "SUP-Z", 7.2, regular_order_qty=2.1, order_multiple=3,
                       regular_order_date=day + timedelta(days=10), seasonal_order=season,
                       order_by=season.deadline, segment=Segment.CONDITIONAL),
        Recommendation("automatic", "SUP-Z", 2.1, order_by=day - timedelta(days=2)),
        Recommendation("unassigned", "", 4, segment=Segment.CONDITIONAL, order_by=day),
        Recommendation("enough", "SUP-A", 0),
    ))
    orders = prepare_orders(facts)
    assert [(batch.quantity, batch.deadline, batch.seasonal) for batch in orders.batches_by_sku["mixed"]] == [
        (6, season.deadline, True), (3, day + timedelta(days=10), False),
    ]
    assert orders.batches_by_sku["enough"] == ()
    supplier, unknown = orders.suppliers
    assert supplier.supplier_id == "SUP-Z"
    assert supplier.quantity == 12
    assert supplier.due and supplier.low_stock
    assert not unknown.due
    assert [(batch.quantity, batch.deadline, batch.seasonal) for batch in supplier.placements] == [
        (3, day, False), (3, day + timedelta(days=10), False), (6, day, True),
    ]
    summary = plan_summary(facts, orders)
    assert (summary.order_now, summary.order_now_units, summary.order_now_suppliers) == (1, 3, 1)
    assert (summary.due_suppliers, summary.decide, summary.order_not_needed) == (1, 2, 1)


def test_grouped_placement_overrides_regular_date_and_deadline_ties_use_supplier_code():
    day = date(2026, 8, 13)
    facts = PlanFacts(day, day, (
        Recommendation("grouped", "SUP-Z", 4, regular_order_qty=4,
                       regular_order_date=day + timedelta(days=40),
                       order_by=day, grouped_season_deadline=day - timedelta(days=5)),
        Recommendation("legacy", "SUP-A", 3),
    ), suppliers=(SupplierSummary("SUP-A", SupplierTiming(30, "История", 60), 3, False, day),))
    orders = prepare_orders(facts)
    assert [supplier.supplier_id for supplier in orders.suppliers] == ["SUP-A", "SUP-Z"]
    assert orders.batches_by_sku["grouped"][0].deadline == day
    assert all(supplier.due for supplier in orders.suppliers)
