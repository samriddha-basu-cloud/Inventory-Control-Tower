"""Digital twin: time-phased Monte-Carlo simulation of the whole network on a *copy* of the operational snapshot.

Production data is never touched: PairInputs are deep-copied, mutated in memory, simulated, and only the aggregated
results are stored (Scenario table). Common random numbers (a per-pair seed derived from the scenario seed) make
baseline and scenario runs directly comparable, so deltas reflect the changes and not sampling noise.

Per time step (1 day, 1 week or 1 month):
    arrivals → demand N(μ·Δ, σ²·Δ) → sales = min(stock, demand) → unmet → review (position ≤ s → order S − position)
Lead times are sampled from the fitted lognormal (or planning value) plus scenario shifts; disruptions delay arrivals.
"""
from __future__ import annotations

import copy
import hashlib
import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from ..extensions import db
from ..models import Scenario
from . import audit_service as audit
from . import carbon_service as carbon
from . import lead_time_service as lts
from . import replenishment_service as rep
from . import safety_stock_service as sss
from . import settings_service as S
from .engine import EngineConfig, PairInputs, PairResult
from .snapshot import Snapshot, get_snapshot

CHANGE_TYPES: dict[str, dict] = {
    "supplier_shutdown": {"label": "Supplier shutdown", "params": [("supplier", "text", "Supplier code"), ("start_day", "number", "Start (day)"), ("duration_days", "number", "Duration (days)")]},
    "plant_shutdown": {"label": "Plant shutdown", "params": [("location", "text", "Location code"), ("start_day", "number", "Start (day)"), ("duration_days", "number", "Duration (days)")]},
    "warehouse_closure": {"label": "Warehouse closure", "params": [("location", "text", "Location code"), ("start_day", "number", "Start (day)"), ("duration_days", "number", "Duration (days)")]},
    "demand_increase": {"label": "Demand increase", "params": [("pct", "number", "Increase %"), ("scope", "text", "SKU / family / all"), ("start_day", "number", "Start (day)"), ("duration_days", "number", "Duration (days)")]},
    "demand_decrease": {"label": "Demand decrease", "params": [("pct", "number", "Decrease %"), ("scope", "text", "SKU / family / all"), ("start_day", "number", "Start (day)"), ("duration_days", "number", "Duration (days)")]},
    "promotion": {"label": "Promotion", "params": [("uplift_pct", "number", "Uplift %"), ("scope", "text", "SKU / family / all"), ("start_day", "number", "Start (day)"), ("duration_days", "number", "Duration (days)")]},
    "lead_time_increase": {"label": "Lead-time increase", "params": [("pct", "number", "Increase %"), ("scope", "text", "Supplier code / SKU / all")]},
    "port_delay": {"label": "Port delay", "params": [("port", "text", "Port name"), ("delay_days", "number", "Delay (days)"), ("start_day", "number", "Start (day)"), ("duration_days", "number", "Duration (days)")]},
    "border_disruption": {"label": "Border disruption", "params": [("country", "text", "Origin country"), ("delay_days", "number", "Delay (days)"), ("start_day", "number", "Start (day)"), ("duration_days", "number", "Duration (days)")]},
    "transport_capacity_reduction": {"label": "Transport capacity reduction", "params": [("pct", "number", "Capacity cut %"), ("start_day", "number", "Start (day)"), ("duration_days", "number", "Duration (days)")]},
    "moq_increase": {"label": "MOQ increase", "params": [("pct", "number", "Increase %"), ("scope", "text", "Supplier code / SKU / all")]},
    "safety_stock_increase": {"label": "Safety stock increase", "params": [("pct", "number", "Increase %"), ("scope", "text", "SKU / family / all")]},
    "service_level_change": {"label": "Service-level change", "params": [("service_level", "number", "New service level (0-1)"), ("scope", "text", "SKU / family / all")]},
    "inventory_transfer": {"label": "Inventory transfer", "params": [("sku", "text", "SKU"), ("from", "text", "From location"), ("to", "text", "To location"), ("qty", "number", "Quantity"), ("day", "number", "Day")]},
    "emergency_purchase": {"label": "Emergency purchase", "params": [("sku", "text", "SKU"), ("location", "text", "Location"), ("qty", "number", "Quantity"), ("lead_days", "number", "Lead time (days)"), ("premium_pct", "number", "Price premium %")]},
    "expedited_shipment": {"label": "Expedited shipment", "params": [("ref", "text", "PO / TO reference"), ("days_earlier", "number", "Days earlier"), ("cost_multiplier", "number", "Freight cost ×")]},
}


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _scope_match(scope, p: PairInputs, snap: Snapshot) -> bool:
    if scope in (None, "", "all", "ALL", "*"):
        return True
    scope = str(scope)
    sup = snap.suppliers.get(p.supplier_id, {})
    return scope in (p.sku, p.item.get("family_code"), p.item.get("industry"), p.item.get("category"), p.loc_code, sup.get("code"), p.loc.get("region"))


@dataclass
class Mods:
    dem: list            # (start_day, end_day, mult)
    lt_shift: float = 0.0
    lt_scale: float = 1.0
    block: list = None   # supply blocks: (start, end) orders/arrivals delayed to `end`
    closure: list = None # demand cannot be served
    delay_rules: list = None   # (predicate(inbound)->bool, days, start, end)
    cap_cut: list = None       # (start, end, pct) arrivals deferred by 7 days
    moq_scale: float = 1.0
    ss_scale: float = 1.0
    sl: float | None = None
    arrivals: list = None      # (day, qty, cost, co2)
    stock_delta: float = 0.0
    extra_expedite: float = 0.0
    extra_co2: float = 0.0
    transfer_cost: float = 0.0
    transit_shift: list = None  # (ref, days_earlier)


def build_mods(p: PairInputs, changes: list[dict], snap: Snapshot) -> Mods:
    m = Mods(dem=[], block=[], closure=[], delay_rules=[], cap_cut=[], arrivals=[], transit_shift=[])
    sup_code = snap.suppliers.get(p.supplier_id, {}).get("code")
    for ch in changes:
        t, a = ch["type"], ch.get("params", {})
        sd, du = _num(a.get("start_day"), 0), _num(a.get("duration_days"), 30)
        if t == "supplier_shutdown":
            if sup_code and sup_code == a.get("supplier"):
                m.block.append((sd, sd + du))
        elif t in ("plant_shutdown", "warehouse_closure"):
            code = a.get("location")
            if p.loc_code == code:
                m.closure.append((sd, sd + du))
                m.block.append((sd, sd + du))
            elif p.source_loc_id and snap.locs.get(p.source_loc_id, {}).get("code") == code:
                m.block.append((sd, sd + du))
        elif t in ("demand_increase", "demand_decrease", "promotion"):
            if _scope_match(a.get("scope"), p, snap):
                pct = _num(a.get("pct", a.get("uplift_pct")), 0.0) / 100.0
                mult = 1 + pct if t != "demand_decrease" else max(0.0, 1 - pct)
                m.dem.append((sd, sd + du, mult))
                if t == "promotion":
                    m.dem.append((sd + du, sd + 2 * du, 0.9))   # post-promotion dip (pull-forward)
        elif t == "lead_time_increase":
            sc = a.get("scope")
            if _scope_match(sc, p, snap):
                m.lt_scale *= 1 + _num(a.get("pct"), 0) / 100.0
        elif t == "port_delay":
            port = a.get("port")
            m.delay_rules.append((lambda ib, port=port: ib.get("port") == port or (port and port in (ib.get("lane") or "")), _num(a.get("delay_days"), 7), sd, sd + du))
        elif t == "border_disruption":
            ctry = a.get("country")
            sp = snap.suppliers.get(p.supplier_id, {})
            if ctry and sp.get("country") == ctry:
                m.delay_rules.append((lambda ib: True, _num(a.get("delay_days"), 7), sd, sd + du))
        elif t == "transport_capacity_reduction":
            m.cap_cut.append((sd, sd + du, _num(a.get("pct"), 30) / 100.0))
        elif t == "moq_increase":
            if _scope_match(a.get("scope"), p, snap):
                m.moq_scale *= 1 + _num(a.get("pct"), 0) / 100.0
        elif t == "safety_stock_increase":
            if _scope_match(a.get("scope"), p, snap):
                m.ss_scale *= 1 + _num(a.get("pct"), 0) / 100.0
        elif t == "service_level_change":
            if _scope_match(a.get("scope"), p, snap):
                m.sl = min(max(_num(a.get("service_level"), 0.95), 0.5), 0.9999)
        elif t == "inventory_transfer":
            if p.sku == a.get("sku"):
                q = _num(a.get("qty"), 0)
                if p.loc_code == a.get("from"):
                    m.stock_delta -= min(q, p.stock.get("UNRESTRICTED", 0.0))
                if p.loc_code == a.get("to"):
                    src = next((snap.locs[l] for l in snap.locs if snap.locs[l]["code"] == a.get("from")), None)
                    dist = _dist(src, p.loc) if src else 0.0
                    days = max(_num(a.get("day"), 0), 0) + carbon.transit_days(dist, "ROAD")
                    m.arrivals.append((days, q, 0.0, carbon.emissions_kg(q * p.item.get("weight_kg", 1.0), dist, "ROAD")))
                    m.transfer_cost += carbon.freight_cost(q * p.item.get("weight_kg", 1.0), dist, "ROAD")
        elif t == "emergency_purchase":
            if p.sku == a.get("sku") and p.loc_code == a.get("location"):
                q = _num(a.get("qty"), 0)
                lead = _num(a.get("lead_days"), 3)
                prem = _num(a.get("premium_pct"), 25) / 100.0
                m.arrivals.append((lead, q, q * p.item.get("unit_cost", 0.0) * prem + carbon.freight_cost(q * p.item.get("weight_kg", 1.0), p.distance_km, "AIR"),
                                   carbon.emissions_kg(q * p.item.get("weight_kg", 1.0), p.distance_km, "AIR")))
                m.extra_expedite += q * p.item.get("unit_cost", 0.0) * prem + carbon.freight_cost(q * p.item.get("weight_kg", 1.0), p.distance_km, "AIR")
                m.extra_co2 += carbon.emissions_kg(q * p.item.get("weight_kg", 1.0), p.distance_km, "AIR")
        elif t == "expedited_shipment":
            ref = a.get("ref")
            for ib in p.inbound:
                if ib["ref"] == ref:
                    m.transit_shift.append((ref, _num(a.get("days_earlier"), 5)))
                    base = carbon.freight_cost(ib["qty"] * p.item.get("weight_kg", 1.0), p.distance_km, p.mode)
                    m.extra_expedite += base * (_num(a.get("cost_multiplier"), 3.0) - 1.0)
                    m.extra_co2 += carbon.emissions_kg(ib["qty"] * p.item.get("weight_kg", 1.0), p.distance_km, "AIR") - carbon.emissions_kg(ib["qty"] * p.item.get("weight_kg", 1.0), p.distance_km, p.mode)
    return m


def _dist(a, b) -> float:
    from ..utils.geo import route_km
    return route_km(a["lat"], a["lon"], b["lat"], b["lon"], "ROAD") if a and b else 0.0


def _pair_seed(seed: int, key: tuple) -> int:
    return int(hashlib.sha1(f"{seed}|{key[0]}|{key[1]}".encode()).hexdigest()[:8], 16)


def simulate_pair(p: PairInputs, r: PairResult, cfg: EngineConfig, mods: Mods, H: int, step: int, runs: int, seed: int) -> dict:
    rng = np.random.default_rng(_pair_seed(seed, p.key))
    n_steps = int(math.ceil(H / step))
    d_daily = r.d_mean
    fc = p.forecast_weekly
    base_daily = np.array([(fc[min(int((t * step) // 7), len(fc) - 1)] / 7.0) if fc else d_daily for t in range(n_steps)])
    firm = np.zeros(n_steps)
    for o in p.orders:
        firm[min(max(int(o["due_day"] // step), 0), n_steps - 1)] += o["qty"]
    for q in p.requirements:
        firm[min(max(int(q["due_day"] // step), 0), n_steps - 1)] += q["qty"]
    mult = np.ones(n_steps)
    for (s0, e0, mm) in mods.dem:
        for t in range(n_steps):
            if s0 <= t * step < e0:
                mult[t] *= mm
    mu = np.maximum(base_daily * step * mult, firm * (mult if mods.dem else 1.0))
    sigma = r.d_std * math.sqrt(step)
    st = lts.compute_stats(p.lt_obs, p.static_lt, cfg.min_lt_obs)
    inv = np.full(runs, max(r.pos["usable_on_hand"] + mods.stock_delta, 0.0))
    backlog = np.zeros(runs)
    arrivals: dict[int, np.ndarray] = defaultdict(lambda: np.zeros(runs))
    pipeline = np.zeros(runs)

    def block_delay(day: float) -> float:
        d = 0.0
        for (s0, e0) in mods.block:
            if s0 <= day < e0:
                d = max(d, e0 - day)
        return d

    for ib in p.inbound:
        eta = ib["eta_day"]
        shift = next((d for ref, d in mods.transit_shift if ref == ib["ref"]), 0.0)
        eta = max(0.0, eta - shift)
        sig = (r.lt_sigma * (0.5 if ib.get("in_transit") else 1.0)) if cfg.use_observed_lt else 0.0
        delay = np.zeros(runs)
        for pred, dd, s0, e0 in mods.delay_rules:
            if pred(ib) and s0 <= eta < e0:
                delay += dd
        eta_run = np.maximum(0.0, eta + (rng.normal(0, sig, runs) if sig > 0 else 0.0) + delay + block_delay(eta) + (mods.lt_shift if not ib.get("in_transit") else 0.0))
        for (s0, e0, pct) in mods.cap_cut:
            hit = (eta_run >= s0) & (eta_run < e0) & (rng.random(runs) < pct)
            eta_run = np.where(hit, eta_run + 7, eta_run)
        idx = np.minimum((eta_run // step).astype(int), n_steps + 5)
        for i in np.unique(idx):
            arrivals[int(i)] += np.where(idx == i, ib["qty"], 0.0)
        pipeline += ib["qty"]
    for (day, q, _c, _e) in mods.arrivals:
        arrivals[int(min(day // step, n_steps + 5))] += q
        pipeline += q
    # policy parameters (scenario-adjusted)
    ss = r.ss * mods.ss_scale
    if mods.sl is not None:
        L, sL = r.lt_plan * mods.lt_scale, r.lt_sigma * mods.lt_scale
        ss = sss.safety_stock(r.ss_method if r.ss_method in sss.METHODS else "combined", d_mean=r.d_mean, d_std=r.d_std, L=L, L_std=sL, service_level=mods.sl,
                              review_days=7, order_qty=r.practical_q or r.eoq).ss * mods.ss_scale
    L_eff = r.lt_plan * mods.lt_scale + mods.lt_shift
    # periodic-review protection: with a review step Δ the protection interval is L + Δ (coarser steps need more cover)
    prot = L_eff + (step if step > 1 else 0)
    ss_step = max(ss, (r.z or 0.0) * math.sqrt(max(prot * r.d_std ** 2 + (r.d_mean * r.lt_sigma * mods.lt_scale) ** 2, 0.0))) * mods.ss_scale \
        if mods.sl is None else ss
    s_pt = r.d_mean * prot + max(ss, ss_step)
    moq = (p.moq or 0) * mods.moq_scale
    Q = max(r.practical_q or 0.0, r.eoq, moq, p.multiple or 1.0)
    reorder = bool(p.supplier_id or p.source_loc_id) and r.d_mean > 0
    lane_info = next((ib for ib in p.inbound if ib.get("lane") or ib.get("port")), {"port": None, "lane": None})
    tot_demand = np.zeros(runs)
    tot_unmet = np.zeros(runs)
    inv_sum = np.zeros(runs)
    orders_qty = np.zeros(runs)
    orders_n = np.zeros(runs)
    ever_out = np.zeros(runs, bool)
    inv_series, out_series = [], []
    for t in range(n_steps):
        day0 = t * step
        a = arrivals.pop(t, None)
        if a is not None:
            inv += a
            pipeline -= a
        d = np.maximum(0.0, rng.normal(mu[t], sigma, runs)) if sigma > 0 else np.full(runs, mu[t])
        closed = any(s0 <= day0 < e0 for (s0, e0) in mods.closure)
        sold = np.zeros(runs) if closed else np.minimum(inv, d)
        inv -= sold
        unmet = d - sold
        backlog += unmet * (1 - cfg.lost_sale_fraction)
        fill = np.minimum(inv, backlog) if not closed else np.zeros(runs)
        inv -= fill
        backlog -= fill
        tot_demand += d
        tot_unmet += unmet
        ever_out |= unmet > 1e-6
        inv_sum += inv
        inv_series.append(float(inv.mean()))
        out_series.append(float((unmet > 1e-6).mean()))
        if reorder:
            pos = inv + pipeline - backlog
            need = pos <= s_pt
            if need.any():
                S_lvl = s_pt + Q
                q_ord = np.where(need, np.maximum(S_lvl - pos, 0.0), 0.0)
                if moq:
                    q_ord = np.where(q_ord > 0, np.maximum(q_ord, moq), 0.0)
                if p.multiple:
                    q_ord = np.where(q_ord > 0, np.ceil(q_ord / p.multiple - 1e-9) * p.multiple, 0.0)
                lt = lts.sample(st, runs, rng, shift=mods.lt_shift, scale=mods.lt_scale) if not (not st.eligible and not st.static) else np.full(runs, L_eff)
                delay = np.array([block_delay(day0 + x) for x in lt]) if mods.block else np.zeros(runs)
                for pred, dd, s0, e0 in mods.delay_rules:
                    if s0 <= day0 < e0 and pred(lane_info):
                        delay = delay + dd
                arr_idx = np.minimum(np.ceil((lt + delay + day0) / step - 1e-9).astype(int), n_steps + 5)
                for i in np.unique(arr_idx[need]):
                    sel = need & (arr_idx == i)
                    arrivals[int(i)] += np.where(sel, q_ord, 0.0)
                pipeline += q_ord
                orders_qty += q_ord
                orders_n += (q_ord > 0)
    horizon_years = H / 365.0
    avg_inv = inv_sum / n_steps
    cost = r.unit_cost
    unmet_units = tot_unmet.mean()
    lost_units = unmet_units * cfg.lost_sale_fraction
    weight = float(p.item.get("weight_kg") or 1.0)
    res = {
        "avg_inv_units": float(avg_inv.mean()), "avg_inv_value": float(avg_inv.mean() * cost), "end_inv_units": float(inv.mean()),
        "end_inv_value": float(inv.mean() * cost), "demand_units": float(tot_demand.mean()), "unmet_units": float(unmet_units),
        "stockout_prob": float(ever_out.mean()), "period_stockout": float(np.mean(out_series)), "service": float(1 - unmet_units / tot_demand.mean()) if tot_demand.mean() > 1e-9 else 1.0,
        "lost_sales_value": float(lost_units * r.price), "revenue_risk": float(unmet_units * r.price), "lost_margin": float(lost_units * max(r.price - cost, 0.0)),
        "production_risk": float(unmet_units * float(p.item.get("line_stop_cost_per_unit") or 0.0)),
        "carrying_cost": float(avg_inv.mean() * cost * cfg.holding_rate * horizon_years),
        "expedite_cost": float(mods.extra_expedite + mods.transfer_cost),
        "carbon_kg": float(mods.extra_co2 + orders_qty.mean() * carbon.emissions_kg(weight, p.distance_km, p.mode)),
        "orders_qty": float(orders_qty.mean()), "orders_n": float(orders_n.mean()), "purchase_value": float(orders_qty.mean() * cost),
        "has_demand": bool(d_daily > 0), "inv_series": inv_series, "out_series": out_series,
    }
    return res


def simulate(snap: Snapshot, changes: list[dict] | None = None, horizon_days: int = 91, step_days: int = 7, runs: int = 40, seed: int = 42,
             pair_keys: list | None = None) -> dict:
    """Run the network simulation on copies of snapshot inputs. Returns aggregate metrics, series and per-pair results."""
    changes = changes or []
    cfg = snap.cfg
    keys = pair_keys or list(snap.inputs.keys())
    n_steps = int(math.ceil(horizon_days / step_days))
    agg = defaultdict(float)
    series_inv = np.zeros(n_steps)
    series_out = np.zeros(n_steps)
    n_dem = 0
    by_pair = []
    for k in keys:
        p = copy.deepcopy(snap.inputs[k])              # the twin never mutates the snapshot itself
        r = snap.results[k]
        mods = build_mods(p, changes, snap)
        res = simulate_pair(p, r, cfg, mods, horizon_days, step_days, runs, seed)
        for f in ("avg_inv_value", "end_inv_value", "avg_inv_units", "unmet_units", "demand_units", "lost_sales_value", "revenue_risk", "lost_margin", "production_risk",
                  "carrying_cost", "expedite_cost", "carbon_kg", "orders_qty", "purchase_value", "orders_n"):
            agg[f] += res[f]
        agg["in_transit_value"] += r.in_transit_value
        if res["has_demand"]:
            n_dem += 1
            agg["stockout_prob_sum"] += res["period_stockout"]
            agg["pairs_at_risk"] += 1 if res["period_stockout"] > 0.10 else 0
        series_inv += np.array(res["inv_series"]) * (r.unit_cost or 0.0)
        series_out += np.array(res["out_series"]) * (1 if res["has_demand"] else 0)
        by_pair.append({"key": k, "sku": p.sku, "node": p.loc_code, "stockout_prob": res["period_stockout"], "service": res["service"], "unmet": res["unmet_units"],
                        "revenue_risk": res["revenue_risk"], "avg_inv_value": res["avg_inv_value"], "end_inv_value": res["end_inv_value"], "has_demand": res["has_demand"]})
    total_out = max(agg["demand_units"], 1e-9)
    metrics = {
        "inventory_value_avg": agg["avg_inv_value"], "inventory_value_end": agg["end_inv_value"], "service_level": 1 - agg["unmet_units"] / total_out,
        "stockout_probability": (agg["stockout_prob_sum"] / n_dem) if n_dem else 0.0, "pairs_at_risk": agg["pairs_at_risk"], "unmet_units": agg["unmet_units"],
        "lost_sales": agg["lost_sales_value"], "revenue_risk": agg["revenue_risk"], "production_risk": agg["production_risk"],
        "working_capital": agg["avg_inv_value"] + agg["in_transit_value"], "carrying_cost": agg["carrying_cost"], "expedite_cost": agg["expedite_cost"],
        "carbon_kg": agg["carbon_kg"], "orders_placed": agg["orders_n"], "purchase_value": agg["purchase_value"], "lost_margin": agg["lost_margin"],
    }
    return {"metrics": metrics, "series": {"inventory_value": series_inv.tolist(), "stockout_pairs": series_out.tolist(),
                                          "labels": [f"{(t + 1) * step_days}d" for t in range(n_steps)]},
            "by_pair": sorted(by_pair, key=lambda x: -x["revenue_risk"])[:60], "step_days": step_days, "horizon_days": horizon_days, "runs": runs, "pairs": len(keys)}


DELTA_METRICS = ["inventory_value_avg", "service_level", "stockout_probability", "pairs_at_risk", "working_capital", "carrying_cost", "expedite_cost", "carbon_kg",
                 "revenue_risk", "production_risk", "lost_sales"]


def compare(baseline: dict, scenario: dict) -> list[dict]:
    rows = []
    for m in DELTA_METRICS:
        b, s = baseline["metrics"][m], scenario["metrics"][m]
        rows.append({"metric": m, "baseline": b, "scenario": s, "delta": s - b, "delta_pct": ((s - b) / b) if b else None})
    return rows


# ---------------------------------------------------------------------------------------------------------- persistence
def run_scenario(name: str, changes: list[dict], *, description: str = "", horizon_days: int = 91, step_days: int = 7, runs: int = 40, seed: int = 42,
                 created_by: str = "system", assumptions: dict | None = None, save: bool = True) -> Scenario:
    snap = get_snapshot()
    for ch in changes:
        if ch["type"] not in CHANGE_TYPES:
            raise ValueError(f"Unknown scenario change type '{ch['type']}'")
    base = simulate(snap, [], horizon_days, step_days, runs, seed)
    scen = simulate(snap, changes, horizon_days, step_days, runs, seed)
    row = Scenario(name=name, description=description, base_dataset=f"production snapshot v{S.data_version()[0]}.{S.data_version()[1]} ({len(snap.inputs)} SKU-locations)",
                   created_by=created_by, time_step_days=step_days, horizon_days=horizon_days, runs=runs, seed=seed,
                   assumptions={"lost_sale_fraction": snap.cfg.lost_sale_fraction, "holding_rate": snap.cfg.holding_rate, "service_level": snap.cfg.service_level,
                                "lt_basis": snap.cfg.lt_basis, "carbon_factors": S.get("carbon.factors"), **(assumptions or {})},
                   changes=changes, status="DONE", is_baseline=not changes,
                   results={"baseline": {"metrics": base["metrics"], "series": base["series"]}, "scenario": {"metrics": scen["metrics"], "series": scen["series"],
                                                                                                          "by_pair": scen["by_pair"]},
                            "deltas": compare(base, scen), "pairs": scen["pairs"]})
    if save:
        db.session.add(row)
        db.session.flush()
        row.scenario_no = f"SCN-{row.id:05d}"
        audit.log("SCENARIO", "Scenario", row.scenario_no, "scenario_run", {"changes": changes, "horizon": horizon_days, "step": step_days, "runs": runs,
                                                                          "production_data_modified": False}, actor=created_by)
    return row
