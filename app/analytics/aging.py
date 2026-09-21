"""Inventory aging & expiry management (sections 19-20)."""
from datetime import date

DEFAULT_AGING_BUCKETS = [(0, 30), (31, 60), (61, 90), (91, 180), (181, 365), (366, None)]


def age_days(received_or_manufacture_date, as_of=None):
    as_of = as_of or date.today()
    if received_or_manufacture_date is None:
        return None
    return (as_of - received_or_manufacture_date).days


def bucket_for_age(days, buckets=None):
    buckets = buckets or DEFAULT_AGING_BUCKETS
    if days is None:
        return "Unknown"
    for lo, hi in buckets:
        if hi is None and days >= lo:
            return f"{lo}+"
        if hi is not None and lo <= days <= hi:
            return f"{lo}-{hi}"
    return "Unknown"


def aging_summary(rows, buckets=None):
    """rows: list of dict(age_days, quantity, unit_cost). Returns bucketed value/qty."""
    buckets = buckets or DEFAULT_AGING_BUCKETS
    summary = {bucket_for_age(0, buckets) if False else None: None}
    summary = {}
    for lo, hi in buckets:
        label = f"{lo}+" if hi is None else f"{lo}-{hi}"
        summary[label] = {"quantity": 0.0, "value": 0.0}

    for r in rows:
        label = bucket_for_age(r["age_days"], buckets)
        if label not in summary:
            summary[label] = {"quantity": 0.0, "value": 0.0}
        summary[label]["quantity"] += r["quantity"]
        summary[label]["value"] += r["quantity"] * r.get("unit_cost", 0.0)

    for v in summary.values():
        v["quantity"] = round(v["quantity"], 2)
        v["value"] = round(v["value"], 2)
    return summary


def expiry_status(expiry_date, as_of=None, near_expiry_days=30):
    as_of = as_of or date.today()
    if expiry_date is None:
        return "N/A"
    remaining = (expiry_date - as_of).days
    if remaining < 0:
        return "EXPIRED"
    if remaining <= near_expiry_days:
        return "NEAR_EXPIRY"
    return "OK"


def remaining_shelf_life_days(expiry_date, as_of=None):
    as_of = as_of or date.today()
    if expiry_date is None:
        return None
    return (expiry_date - as_of).days


def fefo_sort(lots):
    """First-Expiry-First-Out: sort batches by expiry_date ascending. None (no
    expiry tracked) sorts last."""
    return sorted(lots, key=lambda l: (l.get("expiry_date") is None, l.get("expiry_date")))


def fifo_sort(lots):
    """First-In-First-Out: sort by receipt/manufacture date ascending."""
    return sorted(lots, key=lambda l: (l.get("received_date") is None, l.get("received_date")))
