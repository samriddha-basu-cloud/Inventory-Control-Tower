"""Industry profile engine and industry-specific analytics.

Profiles only *configure* the platform (terminology, KPIs, rules, dashboards, alerts, policy overrides, recommended
actions). The core engines are untouched. The functions below feed the Industry Mode page with domain views:
Automotive (ECO/BOM revision, line-side, plant shutdown risk), Pharma (batch/expiry/FEFO/quality), Retail-FMCG
(omnichannel availability, promotions, markdown), High-tech (lifecycle, obsolescence exposure, alternates), Spare parts
(intermittent demand classes) and Manufacturing (WIP, production-order material feasibility).
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import timedelta

from ..extensions import db
from ..models import IndustryProfile, ProductionOrder, Promotion, ReturnRecord
from ..utils.stats import adi_cv2, mean
from . import audit_service as audit
from . import settings_service as S
from .snapshot import Snapshot

PROFILE_FOR_INDUSTRY = {"AUTOMOTIVE": "AUTOMOTIVE", "PHARMA": "PHARMA", "FMCG": "RETAIL_FMCG", "RETAIL": "RETAIL_FMCG", "ELECTRONICS": "HIGH_TECH",
                        "SPARE_PARTS": "SPARE_PARTS", "MANUFACTURING": "MANUFACTURING"}


def profiles() -> list[IndustryProfile]:
    return IndustryProfile.query.order_by(IndustryProfile.name).all()


def active_profile() -> IndustryProfile | None:
    return IndustryProfile.query.filter_by(code=S.active_industry()).first()


def activate(code: str, actor: str) -> IndustryProfile:
    p = IndustryProfile.query.filter_by(code=code).first()
    if not p:
        raise ValueError(f"Unknown industry profile '{code}'")
    S.set_value("industry.active", code, actor=actor)
    audit.log("CONFIG", "IndustryProfile", code, "profile_activated", {"policies": p.policies}, actor=actor)
    return p


def term(key: str, default: str | None = None) -> str:
    p = active_profile()
    return (p.terminology or {}).get(key, default or key) if p else (default or key)


# ---------------------------------------------------------------------------------------------------------- automotive / ECO
def eco_analysis(snap: Snapshot) -> list[dict]:
    """Old-revision stock vs new-revision demand. Recommends Consume / Transfer / Block / Rework / Review - never scrap."""
    rows = []
    sku_to_id = {i["sku"]: iid for iid, i in snap.items.items()}
    for iid, it in snap.items.items():
        succ = it.get("superseded_by_sku")
        if not succ or it.get("eco_day") is None:
            continue
        cut = max(it["eco_day"], 0)
        keys = snap.keys_for_item(iid)
        on_hand = sum(snap.results[k].pos["on_hand"] for k in keys)
        rate = sum(snap.results[k].d_mean for k in keys)
        consume = rate * cut
        leftover = max(0.0, on_hand - consume)
        # nodes short of old revision before the cut-over could absorb stock by transfer
        deficits = []
        for k in keys:
            r = snap.results[k]
            need = r.d_mean * cut
            if need > r.pos["usable_on_hand"] + sum(i["qty"] for i in snap.inputs[k].inbound):
                deficits.append({"node": snap.locs[k[1]]["code"], "deficit": need - r.pos["usable_on_hand"]})
        surplus_nodes = [{"node": snap.locs[k[1]]["code"], "surplus": snap.results[k].pos["usable_on_hand"] - snap.results[k].d_mean * cut}
                         for k in keys if snap.results[k].pos["usable_on_hand"] - snap.results[k].d_mean * cut > 0]
        if leftover <= 0:
            rec, why = "CONSUME", "Forecast consumption before the cut-over covers all stock."
        elif deficits and sum(x["surplus"] for x in surplus_nodes) > 0:
            rec, why = "TRANSFER", f"Move surplus from {', '.join(x['node'] for x in surplus_nodes[:3])} to nodes still consuming this revision ({', '.join(d['node'] for d in deficits[:3])})."
        elif it.get("eco_reworkable"):
            rec, why = "REWORK", "Stock can be reworked to the new revision."
        elif cut <= 7:
            rec, why = "BLOCK", "Cut-over imminent: block leftover stock to prevent use on the new BOM, then review disposition."
        else:
            rec, why = "REVIEW", "Leftover stock after cut-over; engineering/finance review needed. ICT never auto-scraps."
        succ_id = sku_to_id.get(succ)
        rows.append({"item_id": iid, "sku": it["sku"], "revision": it.get("revision"), "successor": succ, "eco_day": cut,
                     "eco_date": snap.today + timedelta(days=cut), "on_hand": on_hand, "consumption_until_eco": consume, "leftover": leftover,
                     "leftover_value": leftover * (it["unit_cost"] or 0), "deficit_nodes": deficits, "surplus_nodes": surplus_nodes, "recommendation": rec,
                     "why": why, "successor_demand_after": sum(snap.results[k].d_mean for k in snap.keys_for_item(succ_id)) if succ_id else None})
    return rows


def plant_shutdown_risk(snap: Snapshot) -> list[dict]:
    """Critical components at plants/line-side likely to stop a line: risk score = production at risk + P(stock-out)."""
    by_loc = defaultdict(list)
    for k, r in snap.results.items():
        inp = snap.inputs[k]
        if inp.loc.get("loc_type") in ("PLANT", "LINE_SIDE") and inp.item.get("criticality") in ("Critical", "High") and (r.d_mean > 0 or inp.requirements):
            by_loc[k[1]].append((inp, r))
    rows = []
    for lid, prs in by_loc.items():
        at_risk = [(i, r) for i, r in prs if r.risk_level in ("HIGH", "CRITICAL")]
        cover = min([r.days_supply for _, r in prs if r.days_supply is not None] or [None]) if prs else None
        rows.append({"location": snap.locs[lid]["code"], "name": snap.locs[lid]["name"], "critical_parts": len(prs), "at_risk_parts": len(at_risk),
                     "min_cover_days": cover, "production_at_risk": sum(r.production_risk for _, r in prs),
                     "worst": ", ".join(i.sku for i, _ in sorted(at_risk, key=lambda t: -t[1].stockout_prob)[:3])})
    rows.sort(key=lambda x: -x["production_at_risk"])
    return rows


def line_side(snap: Snapshot) -> list[dict]:
    out = []
    for k, r in snap.results.items():
        inp = snap.inputs[k]
        if inp.loc.get("loc_type") != "LINE_SIDE":
            continue
        pol = inp.repl
        out.append({"sku": inp.sku, "node": inp.loc_code, "policy": pol.get("policy"), "cards": r.rec.get("planned") and None, "on_hand": r.pos["on_hand"],
                    "cover_hours": (r.days_supply * 24) if r.days_supply is not None else None, "risk": r.risk_level, "criticality": inp.item.get("criticality"),
                    "replenish_qty": r.rec.get("qty")})
    return sorted(out, key=lambda x: (x["cover_hours"] is None, x["cover_hours"] or 0))


# ---------------------------------------------------------------------------------------------------------- pharma
def batch_view(snap: Snapshot) -> list[dict]:
    rows = []
    thr = snap.cfg.expiry_thresholds
    for k, inp in snap.inputs.items():
        if not inp.item.get("lot_tracked"):
            continue
        r = snap.results[k]
        for l in inp.lots:
            if l["qty"] <= 0 or not l.get("lot_no"):
                continue
            dte = (l["expiry"] - snap.today).days if l.get("expiry") else None
            eligible = l["state"] == "UNRESTRICTED" and l.get("quality") == "RELEASED" and (dte is None or dte >= 0)
            rows.append({"sku": inp.sku, "node": inp.loc_code, "lot": l["lot_no"], "qty": l["qty"], "state": l["state"], "quality": l.get("quality"),
                         "received": l.get("received"), "expiry": l.get("expiry"), "days_to_expiry": dte, "eligible": eligible,
                         "temp": inp.item.get("temp_requirement"), "loc_temp": inp.loc.get("temp_capable"),
                         "temp_ok": inp.item.get("temp_requirement") in (None, "AMBIENT") or inp.loc.get("temp_capable") == inp.item.get("temp_requirement"),
                         "value": l["qty"] * r.unit_cost,
                         "status": "EXPIRED" if (dte is not None and dte < 0) else "CRITICAL" if (dte is not None and dte <= thr["critical_days"]) else
                         "NEAR" if (dte is not None and dte <= thr["near_days"]) else "OK"})
    rows.sort(key=lambda x: (x["sku"], x["expiry"] or snap.today + timedelta(days=99999)))
    return rows


# ---------------------------------------------------------------------------------------------------------- retail / fmcg
def omnichannel(snap: Snapshot) -> dict:
    nodes = defaultdict(lambda: {"skus": 0, "available": 0, "value": 0.0, "at_risk": 0, "excess_value": 0.0, "markdown": 0.0})
    for k, r in snap.results.items():
        inp = snap.inputs[k]
        if inp.loc.get("loc_type") not in ("STORE", "DARK_STORE", "MFC", "RDC", "CDC") or inp.item.get("industry") not in ("RETAIL", "FMCG"):
            continue
        n = nodes[inp.loc_code]
        n.update(type=inp.loc.get("loc_type"), channel=inp.loc.get("channel"))
        n["skus"] += 1
        n["available"] += 1 if r.pos["available"] > 0 else 0
        n["value"] += r.on_hand_value
        n["at_risk"] += 1 if r.risk_level in ("HIGH", "CRITICAL") else 0
        n["excess_value"] += r.excess_value
        n["markdown"] += r.excess_value * (inp.item.get("markdown_pct") or 0)
    rows = [{"node": c, **v, "shelf_availability": v["available"] / v["skus"] if v["skus"] else 0.0} for c, v in nodes.items()]
    promos = []
    for p in Promotion.query.filter(Promotion.end_date >= snap.today).all():
        k = (p.item_id, p.location_id)
        r = snap.results.get(k)
        if not r:
            continue
        days = max((p.start_date - snap.today).days, 0)
        uplift = r.d_mean * (1 + p.uplift_pct / 100.0) * ((p.end_date - p.start_date).days + 1)
        cover = r.pos["position"] + sum(i["qty"] for i in snap.inputs[k].inbound if i["eta_day"] <= days)
        promos.append({"promo": p.name, "sku": snap.inputs[k].sku, "node": snap.inputs[k].loc_code, "start": p.start_date, "uplift_pct": p.uplift_pct,
                       "need": uplift, "cover": cover, "gap": max(0.0, uplift - cover), "ok": cover >= uplift})
    returns = ReturnRecord.query.all()
    return {"nodes": sorted(rows, key=lambda x: x["node"]), "promotions": sorted(promos, key=lambda x: -x["gap"]),
            "returns": {"units": sum(r.qty for r in returns), "by_disposition": _count(returns, "disposition")}}


def _count(rows, attr):
    d = defaultdict(float)
    for r in rows:
        d[getattr(r, attr) or "PENDING"] += r.qty
    return dict(d)


# ---------------------------------------------------------------------------------------------------------- high tech
def lifecycle(snap: Snapshot) -> list[dict]:
    rows = []
    for iid, it in snap.items.items():
        if it.get("lifecycle_status") not in ("EOL", "OBSOLETE", "NPI") and not it.get("substitute_group"):
            continue
        keys = snap.keys_for_item(iid)
        rate = sum(snap.results[k].d_mean for k in keys)
        stock = sum(snap.results[k].pos["on_hand"] for k in keys)
        inbound = sum(i["qty"] for k in keys for i in snap.inputs[k].inbound)
        eol = it.get("eol_day")
        need = rate * eol if eol is not None and eol >= 0 else None
        exposure = max(0.0, stock + inbound - need) * (it["unit_cost"] or 0) if need is not None else (stock * (it["unit_cost"] or 0) if it.get("lifecycle_status") == "OBSOLETE" else 0.0)
        runout = (stock + inbound) / rate if rate > 0 else None
        alts = [i["sku"] for j, i in snap.items.items() if j != iid and it.get("substitute_group") and i.get("substitute_group") == it["substitute_group"]]
        rows.append({"sku": it["sku"], "status": it.get("lifecycle_status"), "eol_days": eol, "stock": stock, "inbound": inbound, "daily_demand": rate,
                     "need_until_eol": need, "runout_days": runout, "exposure": exposure, "alternates": alts, "group": it.get("substitute_group"),
                     "action": ("Last-time-buy review" if eol is not None and runout is not None and runout < eol else
                                "Exposure: stop buying / sell-through" if exposure > 0 else "Monitor")})
    return sorted(rows, key=lambda x: -x["exposure"])


# ---------------------------------------------------------------------------------------------------------- spare parts
def intermittent(snap: Snapshot) -> list[dict]:
    rows = []
    for k, inp in snap.inputs.items():
        if inp.item.get("industry") != "SPARE_PARTS":
            continue
        r = snap.results[k]
        adi, cv2 = adi_cv2(inp.hist_weekly[-52:]) if inp.hist_weekly else (float("inf"), 0.0)
        rows.append({"sku": inp.sku, "node": inp.loc_code, "criticality": inp.item.get("criticality"), "ved": inp.item.get("ved"), "profile": r.demand_profile,
                     "adi": None if math.isinf(adi) else adi, "cv2": cv2, "method": r.ss_method, "service_level": r.service_level, "ss": r.ss,
                     "on_hand": r.pos["on_hand"], "lt": r.lt_plan, "class": r.inv_class, "risk": r.risk_level})
    return sorted(rows, key=lambda x: (x["criticality"] != "Critical", x["sku"]))


# ---------------------------------------------------------------------------------------------------------- manufacturing
def production_feasibility(snap: Snapshot) -> list[dict]:
    """For each open production order: can components be covered by available stock + inbound by the start date?"""
    from ..models import BomLine
    boms = defaultdict(list)
    for b in BomLine.query.all():
        boms[b.parent_item_id].append(b)
    rows = []
    for po in ProductionOrder.query.filter(ProductionOrder.status.in_(["OPEN", "RELEASED"])).all():
        open_q = po.qty - po.qty_completed
        start_day = max(((po.start_date or po.due_date) - snap.today).days, 0)
        worst, worst_cov, short = None, None, []
        for b in boms.get(po.item_id, []):
            if b.is_alternate:
                continue
            need = open_q * b.qty_per * (1 + (b.scrap_pct or 0))
            k = (b.component_item_id, po.location_id)
            r = snap.results.get(k)
            if not r:
                short.append((snap.items[b.component_item_id]["sku"], need, 0.0))
                continue
            have = r.pos["usable_on_hand"] + sum(i["qty"] for i in snap.inputs[k].inbound if i["eta_day"] <= start_day) - r.pos["allocated"]
            cov = have / need if need else 1.0
            if worst_cov is None or cov < worst_cov:
                worst, worst_cov = snap.items[b.component_item_id]["sku"], cov
            if cov < 1:
                short.append((snap.items[b.component_item_id]["sku"], need, have))
        rows.append({"order": po.order_number, "sku": po.item.sku, "location": po.location.code, "qty": open_q, "start": po.start_date, "due": po.due_date,
                     "status": "MATERIAL SHORT" if short else "FEASIBLE", "limiting": worst, "coverage": worst_cov,
                     "shortages": [{"sku": s, "need": n, "have": h} for s, n, h in short]})
    return sorted(rows, key=lambda x: (x["status"] != "MATERIAL SHORT", x["due"]))
