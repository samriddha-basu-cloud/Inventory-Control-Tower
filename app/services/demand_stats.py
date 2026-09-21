"""Shared helper: pull demand history for an item/location and compute the
mean/std-dev daily demand figures every optimization module needs."""
import numpy as np
from app.models import DemandHistory


def daily_demand_stats(item_id, location_id, lookback_periods=None):
    q = DemandHistory.query.filter_by(item_id=item_id, location_id=location_id).order_by(
        DemandHistory.period_date
    )
    if lookback_periods:
        rows = q.all()[-lookback_periods:]
    else:
        rows = q.all()
    if not rows:
        return {"avg_demand_daily": 0.0, "std_dev_demand_daily": 0.0, "n_periods": 0,
                "days_since_last_txn": None}
    values = np.array([r.quantity for r in rows], dtype=float)
    from datetime import date
    days_since_last = (date.today() - rows[-1].period_date).days
    return {
        "avg_demand_daily": round(float(np.mean(values)), 3),
        "std_dev_demand_daily": round(float(np.std(values, ddof=1)) if len(values) > 1 else 0.0, 3),
        "n_periods": len(values),
        "days_since_last_txn": days_since_last,
    }


def annual_demand(item_id, location_id):
    stats = daily_demand_stats(item_id, location_id)
    return stats["avg_demand_daily"] * 365
