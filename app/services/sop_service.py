"""S&OP / IBP view: Baseline vs Consensus vs Upside vs Downside over the next six 30-day months.

Demand   = Σ forecast × price (customer-facing item-locations only, so multi-echelon flows are not double counted)
COGS     = Σ forecast × unit cost
Supply   = Σ scheduled external receipts (PO + production orders) × unit cost, by ETA month
Inventory_m = max(0, Inventory_{m-1} + Supply_m − COGS_m);  Gap_m = shortfall below zero (unfunded demand at cost)
Storage utilisation is scaled from today's utilisation by inventory value (an approximation, labelled as such).
Baseline uses BASELINE forecasts only; Consensus uses the effective forecast (ADJUSTED > CONSENSUS > BASELINE);
Upside/Downside scale Consensus by configurable percentages (sop.upside_pct / sop.downside_pct).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from ..models import Forecast
from . import kpi_service
from . import settings_service as S
from .snapshot import Snapshot, week_start

CASES = ["Baseline", "Consensus", "Upside", "Downside"]


def compute(snap: Snapshot, months: int = 6) -> dict:
    up = float(S.get("sop.upside_pct") or 15) / 100.0
    down = float(S.get("sop.downside_pct") or 15) / 100.0
    term = kpi_service.terminal_pairs(snap)
    today = snap.today
    cur_w = week_start(today)

    def month_of(d):
        return max(0, min(months - 1, (d - today).days // 30))

    base_units, base_rev, base_cogs = defaultdict(float), defaultdict(float), defaultdict(float)
    # BASELINE straight from the forecast table
    for f in Forecast.query.filter(Forecast.forecast_type == "BASELINE", Forecast.period_start >= cur_w).all():
        k = (f.item_id, f.location_id)
        if k not in term or k not in snap.results:
            continue
        m = month_of(f.period_start)
        r = snap.results[k]
        if (f.period_start - today).days >= months * 30:
            continue
        base_units[m] += f.qty
        base_rev[m] += f.qty * r.price
        base_cogs[m] += f.qty * r.unit_cost
    cons_units, cons_rev, cons_cogs = defaultdict(float), defaultdict(float), defaultdict(float)
    supply = defaultdict(float)
    for k, inp in snap.inputs.items():
        r = snap.results[k]
        if k in term:
            for w, q in enumerate(inp.forecast_weekly[: months * 4 + 2]):
                m = min(months - 1, (w * 7) // 30)
                cons_units[m] += q
                cons_rev[m] += q * r.price
                cons_cogs[m] += q * r.unit_cost
        for ib in inp.inbound:
            if ib["kind"] in ("PO", "PROD"):
                supply[month_of(today + timedelta(days=max(ib["eta_day"], 0)))] += ib["qty"] * r.unit_cost
    inv0 = sum(r.on_hand_value for r in snap.results.values())
    cap_now = sum((l.get("capacity_units") or 0) for l in snap.locs.values())
    units_now = sum(r.pos["on_hand"] for r in snap.results.values())
    util0 = (units_now / cap_now) if cap_now else None
    hold = snap.cfg.holding_rate
    cases = {}
    for name, (u, rv, cg, mult) in {"Baseline": (base_units, base_rev, base_cogs, 1.0), "Consensus": (cons_units, cons_rev, cons_cogs, 1.0),
                                    "Upside": (cons_units, cons_rev, cons_cogs, 1 + up), "Downside": (cons_units, cons_rev, cons_cogs, 1 - down)}.items():
        rows, inv = [], inv0
        for m in range(months):
            dem_units, dem_rev, cogs = u[m] * mult, rv[m] * mult, cg[m] * mult
            raw = inv + supply[m] - cogs
            gap = min(0.0, raw)
            inv = max(0.0, raw)
            rows.append({"month": m + 1, "label": (today + timedelta(days=30 * m)).strftime("%b %Y"), "demand_units": dem_units, "demand_value": dem_rev, "cogs": cogs,
                         "supply_value": supply[m], "ending_inventory": inv, "gap_value": gap, "margin": dem_rev - cogs, "carrying_cost": inv * hold / 12.0,
                         "utilisation": (util0 * inv / inv0) if (util0 is not None and inv0) else None})
        cases[name] = {"rows": rows, "totals": {"demand_value": sum(r["demand_value"] for r in rows), "margin": sum(r["margin"] for r in rows), "gap_value": sum(r["gap_value"] for r in rows),
                                                "carrying_cost": sum(r["carrying_cost"] for r in rows), "ending_inventory": rows[-1]["ending_inventory"]}}
    return {"cases": cases, "opening_inventory": inv0, "assumptions": {"upside_pct": up, "downside_pct": down, "months": months, "utilisation_now": util0},
            "note": "Utilisation is scaled from today's value-weighted stock and is an approximation."}
