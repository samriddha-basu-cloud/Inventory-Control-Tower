import datetime
from app.optimization.reorder_point import reorder_point, projected_stockout_date


def test_reorder_point_formula():
    result = reorder_point(avg_demand_daily=20, lead_time_days=10, safety_stock=50)
    assert result["lead_time_demand"] == 200
    assert result["reorder_point"] == 250


def test_reorder_point_zero_lead_time():
    result = reorder_point(avg_demand_daily=20, lead_time_days=0, safety_stock=50)
    assert result["reorder_point"] == 50


def test_projected_stockout_date_zero_demand():
    result = projected_stockout_date(100, 0, datetime.date.today())
    assert result["days_to_stockout"] is None
    assert result["stockout_date"] is None


def test_projected_stockout_date_basic():
    today = datetime.date(2026, 1, 1)
    result = projected_stockout_date(100, 10, today)
    assert result["days_to_stockout"] == 10.0
    assert result["stockout_date"] == "2026-01-11"


def test_projected_stockout_date_already_negative_stock():
    today = datetime.date(2026, 1, 1)
    result = projected_stockout_date(-20, 10, today)
    assert result["days_to_stockout"] == -2.0
