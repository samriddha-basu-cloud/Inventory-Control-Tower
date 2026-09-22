"""ICT Inventory Health Index (0–100).

A transparent composite defined by ICT; it is NOT an industry standard. Each component is a 0–100 score obtained
by linear scaling of a measured value between a 'worst' and a 'best' anchor; the index is the weighted mean of the
components that have data (weights are configuration: weights.health).

    Component        measured value                              0 at        100 at
    Availability     share of demand-bearing SKU-locations with usable ≥ safety stock   0 %      100 %
    Service level    line fill in full                                                    80 %     99 %
    Excess           excess value ÷ inventory value                                       30 %      0 %
    Obsolescence     obsolete value ÷ inventory value                                     10 %      0 %
    Accuracy         physical-count accuracy                                              85 %    100 %
    Forecast error   |forecast bias|                                                      25 %      0 %
    Stock-out risk   weighted stock-out probability                                       25 %      0 %
    Aging            share of on-hand value older than 180 days                           40 %      0 %
    Lead time        lead-time reliability (on time vs promised)                          60 %     98 %
"""
from __future__ import annotations

from ..utils.stats import clamp
from . import kpi_service
from . import settings_service as S
from .snapshot import PairFilter, Snapshot

BANDS = [(80, "NORMAL", "Healthy"), (65, "WATCH", "Watch"), (50, "ATTENTION", "Needs attention"), (0, "CRITICAL", "Critical")]

FORMULA_TEXT = ("ICT Inventory Health Index = Σ(weight_i × score_i) ÷ Σ(weight_i) over components with data; "
                "score_i = 100 × clamp((value − worst) ÷ (best − worst), 0, 1). Weights are configurable; this is an ICT-defined index, "
                "not an industry standard.")


def _lin(v, worst, best):
    return 100.0 * clamp((v - worst) / (best - worst))


def band(score: float) -> tuple[str, str]:
    for lo, code, label in BANDS:
        if score >= lo:
            return code, label
    return "CRITICAL", "Critical"


def compute(snap: Snapshot, f: PairFilter | None = None, kpis: dict | None = None) -> dict:
    kpis = kpis or kpi_service.compute_all(snap, f)
    w = S.get("weights.health")
    val = lambda c: (kpis.get(c) or {}).get("value")
    rows = list(snap.rows(f))
    demand_rows = [(i, r) for i, r in rows if r.d_mean > 0]
    comps = []

    def add(key, label, value, score, note):
        comps.append({"key": key, "label": label, "value": value, "score": score, "weight": w.get(key, 0.0),
                      "note": note, "has_data": score is not None})

    avail = (sum(1 for _, r in demand_rows if r.pos["usable_on_hand"] >= r.ss) / len(demand_rows)) if demand_rows else None
    add("availability", "Availability", avail, _lin(avail, 0, 1) if avail is not None else None, "usable ≥ safety stock")
    v = val("service_level")
    add("service", "Service level", v, _lin(v, 0.80, 0.99) if v is not None else None, "lines filled in full")
    v = val("excess_pct")
    add("excess", "Excess", v, _lin(v, 0.30, 0.0) if v is not None else None, "excess value ÷ inventory value")
    v = val("obsolete_pct")
    add("obsolescence", "Obsolescence", v, _lin(v, 0.10, 0.0) if v is not None else None, "obsolete value ÷ inventory value")
    v = val("inventory_accuracy")
    add("accuracy", "Inventory accuracy", v, _lin(v, 0.85, 1.0) if v is not None else None, "physical counts vs ICT")
    v = val("forecast_bias")
    add("forecast", "Forecast error", v, _lin(abs(v), 0.25, 0.0) if v is not None else None, "|forecast bias|")
    v = val("stockout_risk")
    add("stockout_risk", "Stock-out risk", v, _lin(v, 0.25, 0.0) if v is not None else None, "weighted stock-out probability")
    tot_val = sum(r.on_hand_value for _, r in rows) or 0.0
    old = sum(r.on_hand_value for _, r in rows if (r.oldest_days or 0) > 180)
    share = (old / tot_val) if tot_val else None
    add("aging", "Aging", share, _lin(share, 0.40, 0.0) if share is not None else None, "value older than 180 days")
    v = val("lt_reliability")
    add("lead_time", "Lead-time reliability", v, _lin(v, 0.60, 0.98) if v is not None else None, "receipts within promised lead time")
    used = [c for c in comps if c["has_data"]]
    tw = sum(c["weight"] for c in used) or 1.0
    for c in comps:
        c["contribution"] = (c["score"] * c["weight"] / tw) if c["has_data"] else 0.0
    index = sum(c["contribution"] for c in used)
    code, label = band(index)
    return {"index": index, "status": code, "label": label, "components": comps, "formula": FORMULA_TEXT,
            "missing": [c["label"] for c in comps if not c["has_data"]]}


def sku_health(snap: Snapshot, item_id: int) -> dict:
    keys = snap.keys_for_item(item_id)
    rows = [(snap.inputs[k], snap.results[k]) for k in keys]
    d = [(i, r) for i, r in rows if r.d_mean > 0]
    val = sum(r.on_hand_value for _, r in rows)
    parts = {
        "Availability": _lin(sum(1 for _, r in d if r.pos["usable_on_hand"] >= r.ss) / len(d), 0, 1) if d else None,
        "Stock-out risk": _lin(sum(r.stockout_prob for _, r in d) / len(d), 0.25, 0) if d else None,
        "Excess": _lin(sum(r.excess_value for _, r in rows) / val, 0.30, 0) if val else None,
        "Aging": _lin(sum(r.on_hand_value for _, r in rows if (r.oldest_days or 0) > 180) / val, 0.40, 0) if val else None,
    }
    used = [v for v in parts.values() if v is not None]
    idx = sum(used) / len(used) if used else None
    return {"index": idx, "parts": parts, "status": band(idx)[0] if idx is not None else "NODATA"}
