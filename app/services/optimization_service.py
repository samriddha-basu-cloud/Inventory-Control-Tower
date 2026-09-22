"""Optimisation orchestration on top of the snapshot: replenishment MILP, rebalancing LP, decision options,
model-eligibility checks and the decision explainer.
"""
from __future__ import annotations

import copy
import math
from dataclasses import replace
from datetime import timedelta

from ..optimization import network as netopt
from ..optimization import replenishment as repopt
from ..optimization import transfer as trfopt
from ..utils.geo import route_km
from . import carbon_service as carbon
from . import replenishment_service as rep
from . import settings_service as S
from .engine import compute_pair, review_days_for
from .snapshot import PairFilter, Snapshot, get_snapshot

CARBON_PRICE_KEY = "optimization.carbon_price"


def objective_weights() -> dict:
    return S.get("optimization.weights")


def carbon_price() -> float:
    v = S.get(CARBON_PRICE_KEY)
    return 4.0 if v is None else float(v)


# ---------------------------------------------------------------------------------------------------------- explainer
def decision_explainer(inp, r) -> dict:
    """Why an order quantity is what it is - every number dynamically derived from the pair's current state."""
    from .engine import config_from_settings
    L = r.lt_plan
    R = review_days_for(inp, config_from_settings())
    demand = r.d_mean * (L + R)
    available = r.pos["available"]
    inbound = r.pos["in_transit"] + r.pos["on_order"]
    ss = r.ss
    requirement = demand + ss
    shortage = max(0.0, requirement - (available + inbound - r.pos["backorder"]))
    rec = r.rec
    return {
        "headline": f"ORDER {rec['qty']:,.0f} UNITS" if rec.get("qty") else "NO ORDER REQUIRED NOW",
        "rows": [
            ("Projected demand over lead time + review", demand, f"{r.d_mean:,.1f}/day × ({L:.1f} + {R:.0f}) days"),
            ("Current available", available, "usable stock − allocated − committed − reserved"),
            ("Inbound (in-transit + on-order)", inbound, "open POs / transfers / production"),
            ("Backorders", r.pos["backorder"], "past-due unfilled demand"),
            ("Safety stock", ss, f"{r.ss_method} @ {r.service_level:.1%} service level"),
            ("Expected shortage", shortage, "demand + safety stock − (available + inbound − backorders)"),
            ("MOQ", rec["inputs"].get("moq"), "supplier/item minimum order quantity"),
            ("Order multiple", rec["inputs"].get("multiple"), "rounding unit"),
            ("Policy", rec.get("policy_name"), rec.get("explanation", "")),
            ("Recommended quantity", rec.get("qty"), "; ".join(rec.get("notes", [])) or "no rounding applied"),
            ("Expected post-order position", rec.get("post_order_position"), "position + recommended quantity"),
        ],
        "formula": "qty = policy(position, ROP, EOQ, review) → rounded up to MOQ and order multiple; shortage = demand(L+R) + SS − (available + inbound − backorders)",
    }


# ---------------------------------------------------------------------------------------------------------- replenishment MILP
def replenishment_plan(snap: Snapshot | None = None, f: PairFilter | None = None, *, budget=None, warehouse_m3=None, truck_kg=None,
                       service_floor=None, weights=None, enforce_service=None) -> dict:
    snap = snap or get_snapshot()
    cfg = snap.cfg
    weights = weights or objective_weights()
    budget = S.get("optimization.budget") if budget is None else budget
    truck_kg = S.get("optimization.truck_capacity_kg") if truck_kg is None else truck_kg
    warehouse_m3 = S.get("optimization.warehouse_capacity_m3") if warehouse_m3 is None else warehouse_m3
    enforce = S.get("optimization.enforce_service_level") if enforce_service is None else enforce_service
    skus, sup_ids, meta = [], {}, {}
    for k in snap.keys(f):
        inp, r = snap.inputs[k], snap.results[k]
        if not inp.supplier_id or r.d_mean <= 0 or r.unit_cost <= 0:
            continue
        R = review_days_for(inp, cfg)
        target = r.d_mean * (r.lt_plan + R) + r.ss
        need = max(0.0, target - r.pos["position"])
        if need < 1:
            continue
        mult = float(inp.multiple or 1.0)
        moq = float(inp.moq or mult)
        margin = max(r.price - r.unit_cost, r.price * cfg.default_margin_pct)
        horizon_y = (r.lt_plan + R + (r.practical_q / max(r.d_mean, 1e-9)) / 2) / 365.0
        wkg = float(inp.item.get("weight_kg") or 1.0)
        mode = inp.mode or "ROAD"
        std_freight = carbon.freight_cost(wkg * 1000, inp.distance_km, mode) / 1000.0 if wkg else 0.0
        exp_mode = S.get("freight.expedite_mode").get(mode, "AIR")
        exp_freight = carbon.freight_cost(wkg * 1000, inp.distance_km, exp_mode) / 1000.0 if wkg else 0.0
        shelf_cap = None
        if inp.item.get("shelf_life_days"):
            shelf_cap = max(r.d_mean * inp.item["shelf_life_days"] * 0.8, moq)      # never order more than 80 % of shelf-life demand
        cap = inp.supplier_capacity_week * max(r.lt_plan / 7.0, 1) if inp.supplier_capacity_week else None
        skus.append({"id": f"{inp.sku}@{inp.loc_code}", "need": need, "moq": moq, "mult": mult, "max_qty": max(need * 6, moq * 3, mult * 4),
                     "unit_cost": r.unit_cost, "weight_kg": wkg, "volume_m3": float(inp.item.get("volume_m3") or 0.001), "supplier": inp.supplier_id,
                     "holding_unit": r.unit_cost * cfg.holding_rate * horizon_y, "short_cost": margin * cfg.lost_sale_fraction + r.price * 0.03,
                     "expedite_unit": max(exp_freight - std_freight, 0.0) + r.unit_cost * 0.08, "obsolescence_unit": r.unit_cost * 0.05,
                     "co2_unit": carbon.emissions_kg(wkg, inp.distance_km, mode), "shelf_cap": shelf_cap})
        meta[skus[-1]["id"]] = (k, r, inp, cap)
        sup_ids[inp.supplier_id] = (inp.supplier_id, cap)
    suppliers = []
    for sid in sup_ids:
        caps = [m[3] for m in meta.values() if m[2].supplier_id == sid and m[3]]
        suppliers.append({"id": sid, "capacity": sum(caps) / len(caps) if caps else None, "truck_cost": 12000.0})
    cons = {"budget": budget, "warehouse_m3": warehouse_m3, "truck_kg": truck_kg, "service_floor": service_floor or 0.9,
            "order_cost": cfg.ordering_cost, "carbon_price": carbon_price(), "capital_rate": 0.12}
    res = repopt.solve(skus, suppliers, cons, weights, enforce_service=enforce)
    for p in res.get("plan", []):
        k, r, inp, _ = meta[p["id"]]
        p.update(sku=inp.sku, location=inp.loc_code, supplier=snap.suppliers[inp.supplier_id]["name"], position=r.pos["position"],
                 post_position=r.pos["position"] + p["qty"], unit_cost=r.unit_cost, item_id=inp.item_id, location_id=inp.location_id,
                 supplier_id=inp.supplier_id, lead_time=r.lt_plan)
    res["inputs"] = {"skus": len(skus), "suppliers": len(suppliers), "constraints": {k: v for k, v in cons.items() if v is not None}, "weights": weights}
    return res


# ---------------------------------------------------------------------------------------------------------- rebalancing
def rebalancing_candidates(snap: Snapshot, f: PairFilter | None = None) -> list[dict]:
    """Per item: donors (excess beyond safety stock + near-term demand) and receivers (projected shortage)."""
    tpol = S.get("transfer.policy") or {"keep_safety_stock_at_donor": True, "max_transfer_days": 10, "min_transfer_qty": 1, "restricted_lanes": []}
    by_item: dict[int, dict] = {}
    for k in snap.keys(f):
        inp, r = snap.inputs[k], snap.results[k]
        if r.d_mean <= 0 and r.excess_qty <= 0:
            continue
        e = by_item.setdefault(inp.item_id, {"donors": [], "receivers": []})
        R = review_days_for(inp, snap.cfg)
        target_phys = r.d_mean * (r.lt_plan + R) + (r.ss if tpol["keep_safety_stock_at_donor"] else 0.0)
        # surplus vs the node's OWN needs (lead-time + review cover + safety stock), not the 90-day excess threshold
        excess = max(0.0, r.pos["available"] - target_phys - r.d_mean * 2)
        if r.inv_class == "OBSOLETE" or r.pos["usable_on_hand"] <= 0:
            excess = 0.0  # obsolete stock is reviewed by a human, never silently moved
        if excess >= max(tpol["min_transfer_qty"], 1):
            e["donors"].append((k, excess))
        # receiver: expected shortage against target position, and risk >= MEDIUM
        R = review_days_for(inp, snap.cfg)
        target = r.d_mean * (r.lt_plan + R) + r.ss
        gap = target - r.pos["position"]
        if r.d_mean > 0 and (r.risk_level in ("MEDIUM", "HIGH", "CRITICAL") or r.min_projected < 0 or gap > 0.25 * max(target, 1)) and gap > 0:
            e["receivers"].append((k, gap))
    return [{"item_id": i, **v} for i, v in by_item.items() if v["donors"] and v["receivers"]]


def rebalancing_plan(snap: Snapshot | None = None, f: PairFilter | None = None, weights: dict | None = None, require_full: bool = False) -> dict:
    snap = snap or get_snapshot()
    tpol = S.get("transfer.policy") or {"keep_safety_stock_at_donor": True, "max_transfer_days": 10, "min_transfer_qty": 1, "restricted_lanes": []}
    weights = weights or objective_weights()
    cp = carbon_price()
    per_item, meta = [], {}
    for cand in rebalancing_candidates(snap, f):
        iid = cand["item_id"]
        item = snap.items[iid]
        donors = [{"id": snap.locs[k[1]]["code"], "qty": q, "key": k} for k, q in cand["donors"]]
        recvs = []
        for k, gap in cand["receivers"]:
            r = snap.results[k]
            margin = max(r.price - r.unit_cost, r.price * snap.cfg.default_margin_pct)
            recvs.append({"id": snap.locs[k[1]]["code"], "need": gap, "penalty": margin * snap.cfg.lost_sale_fraction * weights.get("stockout", 1.0) + 1.0, "key": k})
        arcs = {}
        for d in donors:
            dl = snap.locs[d["key"][1]]
            for rc in recvs:
                rl = snap.locs[rc["key"][1]]
                if d["id"] == rc["id"]:
                    arcs[(d["id"], rc["id"])] = {"cost": 0, "cap": 0, "blocked": "same node"}
                    continue
                dist = route_km(dl["lat"], dl["lon"], rl["lat"], rl["lon"], "ROAD")
                days = carbon.transit_days(dist, "ROAD")
                w = item["weight_kg"] or 1.0
                unit_cost = carbon.freight_cost(w * 1000, dist, "ROAD", min_charge=0.0) / 1000.0
                co2 = carbon.emissions_kg(w, dist, "ROAD")
                blocked = None
                rr = snap.results[rc["key"]]
                tti = rr.days_to_stockout
                lane = f"{d['id']}->{rc['id']}"
                if lane in tpol.get("restricted_lanes", []):
                    blocked = "lane restricted by transfer policy"
                elif days > tpol["max_transfer_days"]:
                    blocked = f"transit {days:.1f} d exceeds policy maximum {tpol['max_transfer_days']} d"
                elif tti is not None and days > tti + 1e-9:
                    blocked = f"arrives in {days:.1f} d, after projected stock-out in {tti:.0f} d"
                elif dl["temp_capable"] != rl["temp_capable"] and item.get("temp_requirement", "AMBIENT") != "AMBIENT":
                    blocked = "temperature-controlled lane not available"
                arcs[(d["id"], rc["id"])] = {"cost": unit_cost + cp * weights.get("carbon", 0.2) * co2,
                                             "cap": float("inf"), "blocked": blocked, "days": days, "dist": dist, "co2": co2, "freight": unit_cost}
        per_item.append({"item": item["sku"], "donors": donors, "receivers": recvs, "arcs": arcs})
        meta[item["sku"]] = (iid, donors, recvs)
    result = netopt.balance_network(per_item, trfopt.solve_transfers, require_full=require_full)
    rows = []
    for plan in result["plans"]:
        iid, donors, recvs = meta[plan["item"]]
        dmap = {d["id"]: d["key"] for d in donors}
        rmap = {r["id"]: r["key"] for r in recvs}
        for fl in plan["flows"]:
            dk, rk = dmap[fl["donor"]], rmap[fl["receiver"]]
            dr, rr = snap.results[dk], snap.results[rk]
            arc = fl["arc"]
            item = snap.items[iid]
            rows.append({
                "item_id": iid, "sku": plan["item"], "from": fl["donor"], "to": fl["receiver"], "from_id": dk[1], "to_id": rk[1], "qty": fl["qty"],
                "eta_days": arc["days"], "distance_km": arc["dist"], "freight_cost": carbon.freight_cost((item["weight_kg"] or 1) * fl["qty"], arc["dist"], "ROAD"),
                "co2e_kg": carbon.emissions_kg((item["weight_kg"] or 1) * fl["qty"], arc["dist"], "ROAD"),
                "holding_saved": fl["qty"] * dr.unit_cost * snap.cfg.holding_rate * 30 / 365.0,
                "before": {"from_position": dr.pos["position"], "to_position": rr.pos["position"], "from_dos": dr.days_supply, "to_dos": rr.days_supply,
                           "to_risk": rr.risk_level, "to_prob": rr.stockout_prob, "from_prob": dr.stockout_prob},
                "after": _after_transfer(snap, dk, rk, fl["qty"], arc["days"]),
                "value": fl["qty"] * dr.unit_cost,
            })
    unmet = [{"sku": p["item"], "node": rid, "unmet": u} for p in result["plans"] for rid, u in p["unmet"].items() if u > 0.5]
    blocked = [{"sku": p["item"], **b} for p in result["plans"] for b in p["blocked"] if b["reason"] != "same node"]
    expl = [e for p in result["plans"] for e in p["explanation"]]
    return {"status": "INFEASIBLE" if any(p["status"] == "INFEASIBLE" for p in result["plans"]) else "OPTIMAL", "transfers": rows, "unmet": unmet,
            "blocked": blocked, "explanation": expl, "total_cost": result["total_cost"], "items": len(per_item),
            "objective_weights": weights, "constraints": tpol}


def _after_transfer(snap, dk, rk, qty, days) -> dict:
    """Engine re-run on copies of donor and receiver: donor stock reduced now, receiver gets an inbound arrival."""
    cfg = snap.cfg
    di, ri = copy.deepcopy(snap.inputs[dk]), copy.deepcopy(snap.inputs[rk])
    di.stock["UNRESTRICTED"] = di.stock.get("UNRESTRICTED", 0.0) - qty
    ri.inbound.append({"kind": "TO", "ref": "PROPOSED", "qty": qty, "in_transit": False, "eta_day": days, "promised_day": days, "supplier_id": None,
                       "lane": None, "mode": "ROAD", "origin": None, "delay_days": 0})
    dr, rr = compute_pair(di, cfg, with_projection=False), compute_pair(ri, cfg, with_projection=False)
    return {"from_position": dr.pos["position"], "to_position": rr.pos["position"], "from_dos": dr.days_supply, "to_dos": rr.days_supply,
            "to_risk": rr.risk_level, "to_prob": rr.stockout_prob, "from_prob": dr.stockout_prob, "from_risk": dr.risk_level}


# ---------------------------------------------------------------------------------------------------------- decision options
def generate_options(snap: Snapshot, key: tuple) -> dict:
    """Corrective options for one item-location with an engine-computed AFTER state for each.

    Feasibility is checked per option (arrives before stock-out? MOQ/multiple? budget? donor keeps safety stock?)
    and every violation is listed; the best *feasible* option by the visible objective is recommended."""
    inp, r = snap.inputs[key], snap.results[key]
    cfg, item = snap.cfg, inp.item
    w = objective_weights()
    cp = carbon_price()
    budget = S.get("optimization.budget")
    R = review_days_for(inp, cfg)
    target = r.d_mean * (r.lt_plan + R) + r.ss
    gap = max(0.0, target - r.pos["position"])
    tti = r.days_to_stockout
    margin = max(r.price - r.unit_cost, r.price * cfg.default_margin_pct)
    wkg = float(item.get("weight_kg") or 1.0)
    mode = inp.mode or "ROAD"
    options = []
    base_prob, base_short = r.stockout_prob, r.exp_short

    def after_state(mut) -> dict:
        i2 = copy.deepcopy(inp)
        mut(i2)
        r2 = compute_pair(i2, cfg, with_projection=False)
        return {"stockout_prob": r2.stockout_prob, "exp_short": r2.exp_short, "risk_level": r2.risk_level, "position": r2.pos["position"],
                "service_impact": r2.service_impact, "on_hand_value": r2.on_hand_value, "lost_margin": r2.lost_margin, "days_to_stockout": r2.days_to_stockout}

    def finish(opt: dict, aft: dict):
        opt["after"] = aft
        opt["objective"] = _objective(opt, aft, r, cfg, w, cp)
        options.append(opt)

    # 1. standard PO --------------------------------------------------------------------------------------------
    if inp.supplier_id or inp.source_loc_id:
        pq = rep.practical_qty(max(gap, r.rec.get("qty", 0.0)), inp.moq, inp.multiple, inp.repl.get("max_order_qty"))
        qty = pq["qty"]
        if qty > 0:
            src_dist = inp.distance_km
            fr = carbon.freight_cost(wkg * qty, src_dist, mode)
            arr = max(1, math.ceil(r.lt_plan))
            viol = list(pq["violations"])
            if tti is not None and arr > tti:
                viol.append(f"Standard lead time {arr} d is later than the projected stock-out in {tti:.0f} d")
            if budget and qty * r.unit_cost > budget:
                viol.append(f"Purchase value {qty * r.unit_cost:,.0f} exceeds budget {budget:,.0f}")
            if item.get("shelf_life_days") and qty > r.d_mean * item["shelf_life_days"] * 0.8 + 1:
                viol.append("Quantity would outlast shelf life at forecast demand")
            aft = after_state(lambda i: i.inbound.append({"kind": "PO", "ref": "PROPOSED", "qty": qty, "in_transit": False, "eta_day": arr, "promised_day": arr,
                                                          "supplier_id": inp.supplier_id, "lane": None, "mode": mode, "origin": None, "delay_days": 0}))
            src_code = snap.locs[inp.source_loc_id]["code"] if inp.source_loc_id in snap.locs else None
            finish({"type": "CREATE_PO" if inp.supplier_id else "TRANSFER_STOCK",
                    "label": f"Create standard purchase order ({mode.lower()})" if inp.supplier_id else f"Replenish from upstream node {src_code or ''} (standard lead time)".replace("  ", " "), "qty": qty,
                    "arrival_days": arr, "cash_outlay": qty * r.unit_cost, "incremental_cost": cfg.ordering_cost + fr, "carbon_kg": carbon.emissions_kg(wkg * qty, src_dist, mode),
                    "violations": viol, "notes": pq["notes"], "supplier_id": inp.supplier_id, "from_location_id": inp.source_loc_id}, aft)
    # 2. expedite an existing inbound ---------------------------------------------------------------------------
    late = [ib for ib in inp.inbound if ib["kind"] == "PO" and (tti is None or ib["eta_day"] > tti - 0.5)]
    if late:
        ib = max(late, key=lambda x: x["qty"])
        exp_mode = S.get("freight.expedite_mode").get(mode, "AIR")
        cmp_ = carbon.compare_modes(wkg * ib["qty"], inp.distance_km, mode)
        std, exp = cmp_[0], cmp_[1]
        new_eta = max(1, math.ceil(exp["transit_days"]))
        if new_eta < ib["eta_day"]:
            viol = []
            if tti is not None and new_eta > tti:
                viol.append(f"Even expedited arrival ({new_eta} d) is after the projected stock-out ({tti:.0f} d)")
            if exp_mode == mode:
                viol.append("No faster freight mode is configured for this lane")
            aft = after_state(lambda i: [x.update(eta_day=new_eta, promised_day=new_eta) for x in i.inbound if x["ref"] == ib["ref"]])
            finish({"type": "EXPEDITE_PO", "label": f"Expedite {ib['ref']} via {exp_mode.lower()}", "qty": ib["qty"], "arrival_days": new_eta, "cash_outlay": 0.0,
                    "incremental_cost": max(exp["cost"] - std["cost"], 0.0), "carbon_kg": max(exp["co2e_kg"] - std["co2e_kg"], 0.0), "violations": viol,
                    "notes": [f"currently ETA day {ib['eta_day']:+.0f}"], "ref": ib["ref"], "supplier_id": ib.get("supplier_id")}, aft)
    # 3. transfer from a node with excess --------------------------------------------------------------------------
    tpol = S.get("transfer.policy") or {"keep_safety_stock_at_donor": True, "max_transfer_days": 10}
    donors = []
    for k2 in snap.keys_for_item(inp.item_id):
        if k2 == key:
            continue
        dr = snap.results[k2]
        free = dr.pos["available"] - (dr.ss if tpol["keep_safety_stock_at_donor"] else 0.0) - dr.d_mean * 2
        if free > 0 and dr.inv_class != "OBSOLETE":
            dl, rl = snap.locs[k2[1]], snap.locs[key[1]]
            dist = _dist(dl, rl)
            days = carbon.transit_days(dist, "ROAD")
            donors.append((free, k2, dist, days))
    if donors and gap > 0:
        donors.sort(key=lambda t: (carbon.freight_cost(wkg * min(t[0], gap), t[2], "ROAD"), t[3]))
        free, k2, dist, days = donors[0]
        qty = float(math.floor(min(free, max(gap, 1.0))))
        if qty >= 1:
            viol = []
            arr = max(1, math.ceil(days))
            if tti is not None and arr > tti:
                viol.append(f"Transfer arrives in {arr} d, after the projected stock-out in {tti:.0f} d")
            if arr > tpol["max_transfer_days"]:
                viol.append(f"Transit exceeds policy maximum {tpol['max_transfer_days']} d")
            aft = after_state(lambda i: i.inbound.append({"kind": "TO", "ref": "PROPOSED", "qty": qty, "in_transit": False, "eta_day": arr, "promised_day": arr, "supplier_id": None,
                                                          "lane": None, "mode": "ROAD", "origin": None, "delay_days": 0}))
            donor_after = _after_transfer(snap, k2, key, qty, days)
            if donor_after["from_risk"] in ("HIGH", "CRITICAL") and snap.results[k2].risk_level not in ("HIGH", "CRITICAL"):
                viol.append(f"Donor {snap.locs[k2[1]]['code']} would become {donor_after['from_risk']} risk")
            finish({"type": "TRANSFER_STOCK", "label": f"Transfer from {snap.locs[k2[1]]['code']}", "qty": qty, "arrival_days": arr, "cash_outlay": 0.0,
                    "incremental_cost": carbon.freight_cost(wkg * qty, dist, "ROAD"), "carbon_kg": carbon.emissions_kg(wkg * qty, dist, "ROAD"), "violations": viol,
                    "notes": [f"donor excess after safety stock: {free:,.0f}"], "from_location_id": k2[1], "donor_after": donor_after}, aft)
    # 4. substitute / alternate source ------------------------------------------------------------------------------
    if item.get("substitute_group"):
        for iid2, it2 in snap.items.items():
            if iid2 == inp.item_id or it2.get("substitute_group") != item["substitute_group"]:
                continue
            k2 = (iid2, key[1])
            alt_lt = snap.results[k2].lt_plan if k2 in snap.results else float(it2.get("lead_time") or r.lt_plan)
            qty = rep.practical_qty(max(gap, 1.0), it2.get("moq"), it2.get("order_multiple"))["qty"]
            arr = max(1, math.ceil(alt_lt))
            viol = []
            if tti is not None and arr > tti:
                viol.append(f"Alternate lead time {arr} d is after the projected stock-out ({tti:.0f} d)")
            prem = max(((it2.get("unit_cost") or 0) - r.unit_cost), 0) * qty
            aft = after_state(lambda i: i.inbound.append({"kind": "PO", "ref": "ALT-PROPOSED", "qty": qty, "in_transit": False, "eta_day": arr, "promised_day": arr,
                                                          "supplier_id": None, "lane": None, "mode": "AIR", "origin": None, "delay_days": 0}))
            finish({"type": "CREATE_PO", "label": f"Buy qualified alternate {it2['sku']}", "qty": qty, "arrival_days": arr, "cash_outlay": qty * (it2.get("unit_cost") or 0),
                    "incremental_cost": prem + cfg.ordering_cost, "carbon_kg": 0.0, "violations": viol, "notes": ["BOM alternate / substitute group"], "alt_sku": it2["sku"]}, aft)
            break
    # 5. do nothing ------------------------------------------------------------------------------------------------------
    dn_after = {"stockout_prob": base_prob, "exp_short": base_short, "risk_level": r.risk_level, "position": r.pos["position"], "service_impact": r.service_impact,
                "on_hand_value": r.on_hand_value, "lost_margin": r.lost_margin, "days_to_stockout": r.days_to_stockout}
    opt = {"type": "DO_NOTHING", "label": "Do nothing", "qty": 0.0, "arrival_days": None, "cash_outlay": 0.0, "incremental_cost": 0.0, "carbon_kg": 0.0, "violations": [],
           "notes": ["accept the expected loss"]}
    finish(opt, dn_after)
    for o in options:
        o["feasible"] = not o["violations"]
    feasible = sorted([o for o in options if o["feasible"] and o["type"] != "DO_NOTHING"], key=lambda o: o["objective"]["score"])
    best = feasible[0] if feasible else None
    dn = next(o for o in options if o["type"] == "DO_NOTHING")
    if best and dn["objective"]["score"] < best["objective"]["score"] * 0.5 and base_prob < 0.05:
        best = dn
    return {"options": sorted(options, key=lambda o: (not o["feasible"], o["objective"]["score"])), "recommended": best, "gap_qty": gap,
            "escalate": best is None and r.risk_level in ("HIGH", "CRITICAL"), "weights": w,
            "note": None if best else "No option satisfies all constraints - escalate for a manual decision."}


def _dist(a, b):
    return route_km(a["lat"], a["lon"], b["lat"], b["lon"], "ROAD")


def _objective(opt, aft, r, cfg, w, cp) -> dict:
    qty = opt["qty"] or 0.0
    hold_days = max(r.lt_plan, 7) + 14
    holding = qty * r.unit_cost * cfg.holding_rate * hold_days / 365.0
    exp_cost = opt["incremental_cost"]
    carbon_cost = opt.get("carbon_kg", 0.0) * cp
    stock = aft["lost_margin"] + r.production_risk * (aft["exp_short"] / max(r.exp_short, 1e-9) if r.exp_short else 0.0)
    wc = qty * r.unit_cost * 0.12 * hold_days / 365.0
    terms = {"holding": w.get("holding", 1) * holding, "freight & ordering": w.get("expedite", 1) * exp_cost, "expected stock-out cost": w.get("stockout", 1) * stock,
             "carbon (priced)": w.get("carbon", 1) * carbon_cost, "working capital": w.get("working_capital", 1) * wc}
    return {"score": sum(terms.values()), "terms": terms}
