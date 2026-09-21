import datetime
from app.analytics.aging import (
    age_days, bucket_for_age, aging_summary, expiry_status, remaining_shelf_life_days,
    fefo_sort, fifo_sort,
)


def test_age_days_none_date():
    assert age_days(None) is None


def test_bucket_for_age():
    assert bucket_for_age(15) == "0-30"
    assert bucket_for_age(400) == "366+"
    assert bucket_for_age(None) == "Unknown"


def test_aging_summary_buckets_value_and_qty():
    rows = [{"age_days": 10, "quantity": 100, "unit_cost": 5}, {"age_days": 400, "quantity": 20, "unit_cost": 5}]
    summary = aging_summary(rows)
    assert summary["0-30"]["value"] == 500
    assert summary["366+"]["value"] == 100


def test_expiry_status_expired_and_near():
    today = datetime.date(2026, 1, 1)
    assert expiry_status(datetime.date(2025, 12, 1), as_of=today) == "EXPIRED"
    assert expiry_status(datetime.date(2026, 1, 15), as_of=today) == "NEAR_EXPIRY"
    assert expiry_status(datetime.date(2026, 6, 1), as_of=today) == "OK"
    assert expiry_status(None) == "N/A"


def test_remaining_shelf_life_none():
    assert remaining_shelf_life_days(None) is None


def test_fefo_sort_orders_by_earliest_expiry_first():
    lots = [
        {"batch": "B", "expiry_date": datetime.date(2026, 6, 1)},
        {"batch": "A", "expiry_date": datetime.date(2026, 1, 1)},
        {"batch": "C", "expiry_date": None},
    ]
    result = fefo_sort(lots)
    assert [l["batch"] for l in result] == ["A", "B", "C"]


def test_fifo_sort_orders_by_received_date():
    lots = [
        {"batch": "B", "received_date": datetime.date(2026, 3, 1)},
        {"batch": "A", "received_date": datetime.date(2026, 1, 1)},
    ]
    result = fifo_sort(lots)
    assert [l["batch"] for l in result] == ["A", "B"]
