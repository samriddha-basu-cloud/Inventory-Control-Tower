"""Shelf-life, expiry status, FEFO picking, expiry-at-risk stock, and aging buckets."""
from __future__ import annotations

from datetime import date, timedelta

from ..rules.thresholds import expiry_status


def expiry_info(expiry: date | None, mfg: date | None, shelf_life_days: int | None, today: date, th: dict) -> dict:
    """Days to expiry, % shelf life remaining and status (OK / NEAR / CRITICAL / EXPIRED / NONE)."""
    if not expiry:
        return {"days_to_expiry": None, "pct_remaining": None, "status": "NONE"}
    dte = (expiry - today).days
    total = None
    if mfg:
        total = (expiry - mfg).days
    elif shelf_life_days:
        total = shelf_life_days
    pct = None if not total or total <= 0 else max(0.0, min(1.0, dte / total))
    return {"days_to_expiry": dte, "pct_remaining": pct, "status": expiry_status(dte, pct, th)}


def fefo_sort(lots: list[dict]) -> list[dict]:
    """First-Expiry-First-Out: earliest expiry first; lots without expiry last (FIFO by receipt as tiebreak)."""
    far = date.max
    return sorted(lots, key=lambda l: (l.get("expiry") or far, l.get("received") or far, l.get("lot_no", "")))


def fefo_pick(lots: list[dict], qty: float, today: date, *, require_released: bool = True,
              min_remaining_days: int = 0, allow_expired: bool = False) -> dict:
    """Plan a pick of `qty` by FEFO. Only RELEASED, non-expired lots are eligible unless policy relaxes it."""
    remaining = float(qty)
    picks, skipped = [], []
    for l in fefo_sort(lots):
        q = float(l.get("qty", 0))
        if q <= 0:
            continue
        reason = None
        if require_released and l.get("quality", "RELEASED") != "RELEASED":
            reason = f"quality status {l.get('quality')}"
        elif l.get("state", "UNRESTRICTED") != "UNRESTRICTED":
            reason = f"stock state {l.get('state')}"
        elif l.get("expiry") and not allow_expired and (l["expiry"] - today).days < max(min_remaining_days, 0):
            reason = "expired" if l["expiry"] < today else f"less than {min_remaining_days} days remaining"
        if reason:
            skipped.append({"lot_no": l.get("lot_no"), "qty": q, "reason": reason})
            continue
        take = min(q, remaining)
        picks.append({"lot_no": l.get("lot_no"), "lot_id": l.get("lot_id"), "qty": take, "expiry": l.get("expiry")})
        remaining -= take
        if remaining <= 1e-9:
            break
    return {"picks": picks, "picked": float(qty) - max(remaining, 0), "short": max(remaining, 0), "skipped": skipped}


def expiry_at_risk(lots: list[dict], daily_demand: float, today: date) -> list[dict]:
    """Quantity in each lot that cannot be consumed before it expires, assuming FEFO consumption at the forecast rate.

    Cumulative FEFO consumption capacity up to a lot's expiry = daily_demand × days_to_expiry. Anything in the
    cumulative stock beyond that capacity is at risk."""
    res = []
    cum_before = 0.0
    for l in fefo_sort(lots):
        q = float(l.get("qty", 0))
        if q <= 0:
            continue
        exp = l.get("expiry")
        if not exp:
            cum_before += q
            continue
        dte = (exp - today).days
        capacity = max(0.0, daily_demand * max(dte, 0))
        consumable = max(0.0, min(q, capacity - cum_before))
        risk_qty = q - consumable
        res.append({**l, "days_to_expiry": dte, "at_risk_qty": risk_qty, "consumable_qty": consumable})
        cum_before += q
    return res


def aging_buckets(edges: list[int]) -> list[tuple[str, int, int | None]]:
    """Edges [30,60,90,180,365] -> [('0–30',0,30),('31–60',31,60),...,('365+',366,None)]. Custom edges supported."""
    edges = sorted({int(e) for e in edges if e and int(e) > 0})
    out, lo = [], 0
    for e in edges:
        out.append((f"{lo}–{e}", lo, e))
        lo = e + 1
    out.append((f"{edges[-1] if edges else 0}+", lo, None))
    return out


def bucket_for(age_days: float, buckets: list[tuple[str, int, int | None]]) -> str:
    a = max(0, int(age_days))
    for label, lo, hi in buckets:
        if a >= lo and (hi is None or a <= hi):
            return label
    return buckets[-1][0]
