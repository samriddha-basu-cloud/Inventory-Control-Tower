"""Configurable threshold logic (risk levels, expiry buckets, KPI status)."""
from __future__ import annotations

RISK_ORDER = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
STATUS_ORDER = ["NORMAL", "WATCH", "ATTENTION", "CRITICAL"]


def stockout_level(prob: float, days_to_stockout: float | None, lead_time_days: float, th: dict) -> str:
    """Probability bands from `thresholds.stockout`, escalated one band when the projected stockout falls
    inside the replenishment lead time (no time left to react with a standard order)."""
    if prob >= th["critical"]:
        level = 3
    elif prob >= th["high"]:
        level = 2
    elif prob >= th["medium"]:
        level = 1
    else:
        level = 0
    if days_to_stockout is not None and days_to_stockout <= lead_time_days and level >= 1:
        level = min(3, level + 1)
    return RISK_ORDER[level]


def expiry_status(days_to_expiry: float | None, pct_remaining: float | None, th: dict) -> str:
    if days_to_expiry is None:
        return "NONE"
    if days_to_expiry < 0:
        return "EXPIRED"
    if days_to_expiry <= th["critical_days"]:
        return "CRITICAL"
    if days_to_expiry <= th["near_days"] or (pct_remaining is not None and pct_remaining <= th.get("near_pct", 0)):
        return "NEAR"
    return "OK"


def kpi_status(value: float | None, direction: str, watch, attention, critical) -> str:
    """Four-level status. `direction`='higher' means larger is better: value below `watch` -> WATCH, etc."""
    if value is None or watch is None:
        return "NORMAL"
    if direction == "higher":
        if critical is not None and value < critical:
            return "CRITICAL"
        if attention is not None and value < attention:
            return "ATTENTION"
        if value < watch:
            return "WATCH"
    else:
        if critical is not None and value > critical:
            return "CRITICAL"
        if attention is not None and value > attention:
            return "ATTENTION"
        if value > watch:
            return "WATCH"
    return "NORMAL"


def worse(a: str, b: str, order=RISK_ORDER) -> str:
    return a if order.index(a) >= order.index(b) else b
