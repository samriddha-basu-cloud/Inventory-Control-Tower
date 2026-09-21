import datetime
from app.optimization.allocation import fifo_allocate, priority_allocate, proportional_allocate, margin_allocate


def _demands():
    return [
        {"id": 1, "quantity": 100, "order_date": datetime.date(2026, 1, 3), "priority_weight": 1.0, "margin_per_unit": 10},
        {"id": 2, "quantity": 50, "order_date": datetime.date(2026, 1, 1), "priority_weight": 3.0, "margin_per_unit": 30},
        {"id": 3, "quantity": 80, "order_date": datetime.date(2026, 1, 2), "priority_weight": 2.0, "margin_per_unit": 5},
    ]


def test_fifo_serves_earliest_order_first():
    result = fifo_allocate(_demands(), available_supply=120)
    by_id = {r["id"]: r for r in result["allocations"]}
    assert by_id[2]["allocated_quantity"] == 50  # earliest order date
    assert by_id[3]["allocated_quantity"] == 70  # remaining 70 of 120
    assert by_id[1]["allocated_quantity"] == 0


def test_priority_serves_highest_weight_first():
    result = priority_allocate(_demands(), available_supply=50)
    by_id = {r["id"]: r for r in result["allocations"]}
    assert by_id[2]["allocated_quantity"] == 50  # highest priority weight
    assert by_id[1]["allocated_quantity"] == 0
    assert by_id[3]["allocated_quantity"] == 0


def test_proportional_fair_share():
    result = proportional_allocate(_demands(), available_supply=115)  # 50% of 230 total demand
    assert result["fill_ratio_pct"] == 50.0
    by_id = {r["id"]: r for r in result["allocations"]}
    assert by_id[1]["allocated_quantity"] == 50.0


def test_proportional_zero_demand():
    result = proportional_allocate([], available_supply=100)
    assert result["total_demand"] == 0


def test_margin_allocate_prioritizes_highest_margin():
    result = margin_allocate(_demands(), available_supply=50)
    by_id = {r["id"]: r for r in result["allocations"]}
    assert by_id[2]["allocated_quantity"] == 50  # highest margin_per_unit


def test_allocation_never_exceeds_supply():
    result = fifo_allocate(_demands(), available_supply=1000)
    assert result["total_allocated"] == result["total_demand"]


def test_allocation_zero_supply():
    result = fifo_allocate(_demands(), available_supply=0)
    assert result["total_allocated"] == 0
    assert result["total_unfulfilled"] == result["total_demand"]
