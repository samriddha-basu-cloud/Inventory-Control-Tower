"""Time-phased demand–supply balance and probabilistic stock-out risk.

Projection (per period, gross-to-net):

    Opening
    + Receipts (PO / production / transfers in, dated by ETA, promised date, or a P90-delayed ETA)
    − Order demand (open sales orders — already allocated demand)
    − Unallocated forecast   = max(0, forecast − orders in the same week)   ← forecast consumption, avoids double count
    − Production consumption (BOM-dependent requirements)
    − Transfers out
    = Ending projected inventory       Gap = Ending − Safety stock

Existing stock allocated to orders is *not* subtracted a second time: those orders are the "order demand" row.
Opening stock is usable on-hand (gross).

Stock-out probability over a horizon T (mixture over arrival scenarios, normal demand):
    Supply(T)  = usable + Σ qᵢ·1[line i arrives by T],    P(arrive by T) = Φ((T − ETAᵢ)/σᵢ)
    Demand(T)  ~ N(μ_D, σ_d²·T)
    P(stockout) = Σ_scenarios P(s)·Φ(−z_s)  and  E[shortage] = Σ P(s)·σ·G(z_s)   (G = normal loss function)
"""
from __future__ import annotations

import math

import numpy as np
from scipy.stats import norm

from ..utils.stats import normal_loss
from .lead_time_service import prob_arrival_by


def _daily_arrays(H, forecast_daily, order_demand, requirements, inbound, transfers_out, eta_mode, backorder):
    fc = np.zeros(H)
    src = list(forecast_daily or [])
    for i in range(H):
        fc[i] = src[i] if i < len(src) else (src[-1] if src else 0.0)
    orders = np.zeros(H)
    for o in order_demand or []:
        day = max(0, int(math.floor(o["due_day"])))
        if day < H:
            orders[day] += o["qty"]
    if backorder and H:
        orders[0] += backorder
    req = np.zeros(H)
    for r in requirements or []:
        day = max(0, int(math.floor(r["due_day"])))
        if day < H:
            req[day] += r["qty"]
    out = np.zeros(H)
    for t in transfers_out or []:
        day = max(0, int(math.floor(t["day"])))
        if day < H:
            out[day] += t["qty"]
    rec = np.zeros(H)
    for i in inbound or []:
        if eta_mode == "promised" and i.get("promised_day") is not None:
            d = i["promised_day"]
        elif eta_mode == "p90":
            d = i["eta_day"] + 1.2816 * i.get("sigma_days", 0.0)
        else:
            d = i["eta_day"]
        day = max(0, int(math.ceil(d)))
        if day < H:
            rec[day] += i["qty"]
    # forecast consumption at weekly granularity
    unalloc = np.zeros(H)
    for w0 in range(0, H, 7):
        w1 = min(H, w0 + 7)
        left = max(0.0, fc[w0:w1].sum() - orders[w0:w1].sum())
        unalloc[w0:w1] = left / (w1 - w0)
    return rec, orders, unalloc, req, out


def project(opening: float, forecast_daily, order_demand, requirements, inbound, transfers_out, safety_stock: float,
            horizon_days: int = 91, step_days: int = 7, eta_mode: str = "eta", backorder: float = 0.0) -> dict:
    H = int(horizon_days)
    rec, orders, unalloc, req, out = _daily_arrays(H, forecast_daily, order_demand, requirements, inbound,
                                                    transfers_out, eta_mode, backorder)
    demand = orders + unalloc + req + out
    cum = opening + np.cumsum(rec - demand)
    rows = []
    n_periods = int(math.ceil(H / step_days))
    for k in range(n_periods):
        s, e = k * step_days, min(H, (k + 1) * step_days)
        op = float(opening if s == 0 else cum[s - 1])
        end = float(cum[e - 1])
        gap = end - safety_stock
        flag = "STOCKOUT" if end < -1e-9 else ("BELOW_SS" if gap < -1e-9 else "OK")
        rows.append({
            "period": k + 1, "start_day": s, "end_day": e,
            "opening": op, "receipts": float(rec[s:e].sum()),
            "order_demand": float(orders[s:e].sum()), "forecast_demand": float(unalloc[s:e].sum()),
            "production_consumption": float(req[s:e].sum()), "transfers_out": float(out[s:e].sum()),
            "ending": end, "safety_stock": safety_stock, "gap": gap, "flag": flag,
        })
    neg = np.where(cum < -1e-9)[0]
    below = np.where(cum < safety_stock - 1e-9)[0]
    return {
        "rows": rows,
        "daily_ending": cum.tolist(),
        "daily_demand": demand.tolist(),
        "first_stockout_day": int(neg[0]) if neg.size else None,
        "first_below_ss_day": int(below[0]) if below.size else None,
        "min_ending": float(cum.min()) if H else float(opening),
        "expected_shortage": float(max(0.0, -cum.min())) if H else 0.0,
        "shortage_days": int(neg.size),
        "total_demand": float(demand.sum()),
        "total_receipts": float(rec.sum()),
    }


def stockout_probability(usable: float, inbound: list[dict], demand_mean_T: float, demand_std_T: float, T: float,
                         safety_stock: float = 0.0, backorder: float = 0.0) -> dict:
    """See module docstring. inbound: [{qty, eta_day, sigma_days}].

    Arrival uncertainty is handled as a *mixture*: the (up to) four inbound lines with the most arrival uncertainty are
    enumerated (arrives / does not arrive by T, independent), every other line contributes its expected quantity.
    Within each scenario demand is normal, so P(stock-out) = Σ P(scenario)·Φ(−z_s) and E[shortage] = Σ P(scenario)·σ·G(z_s).
    Unlike a single normal approximation this is monotone: adding supply can never raise the stock-out probability.
    """
    probs = [(i, prob_arrival_by_safe(T, i)) for i in inbound]
    uncertain = sorted([(i, p) for i, p in probs if 0.005 < p < 0.995], key=lambda t: -t[0]["qty"] * t[1] * (1 - t[1]))[:4]
    unc_ids = {id(i) for i, _ in uncertain}
    fixed_supply = float(usable) + sum(i["qty"] * p for i, p in probs if id(i) not in unc_ids)
    scenarios = [(fixed_supply, 1.0)]
    for i, p in uncertain:
        scenarios = [(sv + (i["qty"] if arrives else 0.0), w * (p if arrives else 1 - p)) for sv, w in scenarios for arrives in (True, False)]
    sig = max(demand_std_T, 0.0)
    prob = short = p_ss = 0.0
    for sv, w in scenarios:
        net = sv - demand_mean_T - backorder
        if sig < 1e-9:
            prob += w * (1.0 if net < 0 else 0.0)
            short += w * max(0.0, -net)
            p_ss += w * (1.0 if net < safety_stock else 0.0)
        else:
            z = net / sig
            prob += w * float(norm.cdf(-z))
            short += w * float(sig * normal_loss(z))
            p_ss += w * float(norm.cdf(-(net - safety_stock) / sig))
    exp_supply = sum(w * sv for sv, w in scenarios)
    return {"probability": min(max(prob, 0.0), 1.0), "p_below_safety": min(max(p_ss, 0.0), 1.0), "expected_shortage": short, "expected_supply": exp_supply,
            "demand_mean": demand_mean_T, "sigma_total": sig,
            "service_impact": (short / demand_mean_T) if demand_mean_T > 1e-9 else 0.0}


def prob_arrival_by_safe(T: float, i: dict) -> float:
    return prob_arrival_by(T, i["eta_day"], i.get("sigma_days", 0.0))
