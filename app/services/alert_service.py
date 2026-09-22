"""Alert generation from data-driven rules, with alert-fatigue controls.

Pipeline:  build metric contexts → evaluate Rule rows → de-duplicate on a stable key → impact estimate → transparent
Planner Priority Score → suppression → escalation → auto-resolve stale alerts → root-cause clustering (incident_service).

Planner Priority Score (0–100) = Σ wᵢ·sᵢ / Σ wᵢ with sub-scores 0–100 and configurable weights (weights.priority):
    financial  = 100·min(1, log10(1+value at risk) / log10(1+5e7))            (₹5 Cr ⇒ 100)
    service    = min(100, 400 × service-level impact)                         (25 % of demand short ⇒ 100)
    production = 100 for critical parts with line-stop exposure, else scaled log10 of production risk
    customer   = min(100, 25 × (customers affected + contract customers))
    severity   = LOW 15 / MEDIUM 40 / HIGH 70 / CRITICAL 100
    time       = 100·exp(−days to impact ÷ 14)   (no date ⇒ 20)
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timedelta

from ..extensions import db
from ..models import Alert, IndustryProfile, Incident, InventoryBalance, Lot, Rule
from ..rules.rule_engine import eval_condition, severity_for
from ..utils.stats import mean, std
from . import audit_service as audit
from . import incident_service, reconciliation_service, risk_service
from . import settings_service as S
from .snapshot import Snapshot, get_snapshot

SEV_SCORE = {"LOW": 15, "MEDIUM": 40, "HIGH": 70, "CRITICAL": 100}
RISK_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
STATUSES = ["New", "Acknowledged", "Investigating", "Action Proposed", "Approved", "Executed", "Resolved", "Dismissed"]
OPEN_STATUSES = ["New", "Acknowledged", "Investigating", "Action Proposed", "Approved"]
OWNERS = {"STOCKOUT": "Inventory Planner", "SAFETY_STOCK_BREACH": "Inventory Planner", "EXCESS": "Supply Chain Manager", "EXPIRY": "Warehouse Manager",
          "OBSOLESCENCE": "Supply Chain Manager", "AGING": "Warehouse Manager", "PO_DELAY": "Procurement", "SUPPLIER_DELAY": "Procurement",
          "ETA_CHANGE": "Procurement", "INVENTORY_VARIANCE": "Warehouse Manager", "DEMAND_SPIKE": "Demand Planner", "DEMAND_DROP": "Demand Planner",
          "LEAD_TIME_INCREASE": "Procurement", "CAPACITY_BREACH": "Warehouse Manager", "ALLOCATION_CONFLICT": "Inventory Planner",
          "QUALITY_HOLD": "Warehouse Manager", "DATA_STALE": "Administrator"}


def priority_score(impact: dict, severity: str, tti: float | None, weights: dict) -> tuple[float, dict]:
    val = max(impact.get("value_at_risk", 0.0), impact.get("revenue_at_risk", 0.0), 0.0)
    prod = impact.get("production_at_risk", 0.0)
    subs = {
        "financial": 100 * min(1.0, math.log10(1 + val) / math.log10(1 + 5e7)),
        "service": min(100.0, 400.0 * impact.get("service_impact", 0.0)),
        "production": 100.0 if (impact.get("critical") and prod > 0) else (min(100.0, 100 * math.log10(1 + prod) / math.log10(1 + 5e6)) if prod > 0 else 0.0),
        "customer": min(100.0, 25.0 * (impact.get("customers", 0) + impact.get("contract_customers", 0))),
        "severity": float(SEV_SCORE.get(severity, 40)),
        "time": 20.0 if tti is None else 100.0 * math.exp(-max(tti, 0.0) / 14.0),
    }
    tw = sum(weights.values()) or 1.0
    parts = {k: {"score": subs[k], "weight": weights.get(k, 0.0), "contribution": subs[k] * weights.get(k, 0.0) / tw} for k in subs}
    return sum(p["contribution"] for p in parts.values()), parts


# ---------------------------------------------------------------------------------------------------------- contexts
def _demand_signal(hist: list[float]) -> tuple[float, float]:
    if len(hist) < 20:
        return 1.0, 0.0
    recent, prior = hist[-4:], hist[-26:-4] if len(hist) >= 26 else hist[:-4]
    pm, ps = mean(prior), std(prior)
    if pm <= 0:
        return 1.0, 0.0
    rm = mean(recent)
    z = (rm - pm) / (ps / math.sqrt(4)) if ps > 0 else 0.0
    return rm / pm, z


def pair_context(snap: Snapshot, inp, r) -> dict:
    p = r.pos
    item = inp.item
    ss = max(r.ss, 0.0)
    ratio_lt = (r.lt_stats.get("mean") / r.lt_static) if (r.lt_stats.get("eligible") and r.lt_static and r.lt_stats.get("mean")) else None
    ratio, z = _demand_signal(inp.hist_weekly)
    cover_days = r.lt_plan
    due_lt = sum(o["qty"] for o in inp.orders if o["due_day"] <= cover_days)
    eco_leftover = None
    if item.get("eco_day") and item.get("superseded_by_sku"):
        cut = max(item["eco_day"], 0)
        consumed = r.d_mean * cut
        eco_leftover = max(0.0, p["on_hand"] - consumed)
    eol_exposure = 0.0
    if item.get("eol_day") is not None and item["eol_day"] >= 0 and item.get("lifecycle_status") in ("EOL",):
        need_until_eol = r.d_mean * item["eol_day"]
        eol_exposure = max(0.0, p["on_hand"] + sum(i["qty"] for i in inp.inbound) - need_until_eol) * r.unit_cost
    return {
        "lt_horizon": r.lt_plan + float(inp.repl.get("review_days") or snap.cfg.review_days), "risk_rank": RISK_RANK[r.risk_level], "risk_level": r.risk_level, "projected_min": r.min_projected, "stockout_prob": r.stockout_prob,
        "days_to_stockout": r.days_to_stockout, "usable_on_hand": p["usable_on_hand"], "safety_stock": ss,
        "ss_ratio": (p["usable_on_hand"] / ss) if ss > 0 else 9.9, "demand_daily": r.d_mean, "excess_value": r.excess_value, "inv_class": r.inv_class,
        "on_hand_value": r.on_hand_value, "oldest_days": r.oldest_days or 0, "expiry_status": r.expiry_status, "at_risk_qty": r.at_risk_qty + r.expired_qty,
        "lt_ratio": ratio_lt, "lt_obs_n": r.lt_stats.get("n", 0), "demand_ratio_4w": ratio, "demand_z": z, "over_allocated": p["over_allocated"],
        "unallocated_due_in_lt": due_lt, "available_pos": max(p["available"], 0.0) + sum(i["qty"] for i in inp.inbound if i["eta_day"] <= cover_days),
        "eco_leftover_qty": eco_leftover, "eol_exposure_value": eol_exposure,
    }


def _pair_impact(snap, inp, r, atype: str, ctx: dict) -> tuple[dict, float | None]:
    contract_set = {cid for cid, c in snap.customers.items() if c.get("contract_priority")}
    cs = {o["customer_id"] for o in inp.orders if o["due_day"] <= max(r.lt_plan, 7) and o.get("customer_id")} | \
         {o["customer_id"] for o in inp.extra.get("backorders", []) if o.get("customer_id")}
    imp = {"value_at_risk": 0.0, "revenue_at_risk": 0.0, "production_at_risk": 0.0, "service_impact": 0.0, "customers": len(cs),
           "contract_customers": len(cs & contract_set), "critical": inp.item.get("criticality") == "Critical", "inventory_impact": 0.0}
    tti = None
    if atype in ("STOCKOUT", "SAFETY_STOCK_BREACH", "ALLOCATION_CONFLICT", "DEMAND_SPIKE"):
        det = r.expected_shortage_qty * r.price
        imp["revenue_at_risk"] = max(r.lost_sales_value, det * snap.cfg.lost_sale_fraction)
        imp["production_at_risk"] = r.production_risk + (r.expected_shortage_qty * float(inp.item.get("line_stop_cost_per_unit") or 0) if r.expected_shortage_qty else 0)
        imp["service_impact"] = max(r.service_impact, (r.expected_shortage_qty / max(r.d_mean * r.lt_plan, 1e-9)) if r.d_mean else 0.0)
        imp["value_at_risk"] = imp["revenue_at_risk"]
        tti = r.days_to_stockout
        if tti is None and r.d_mean > 0:
            tti = r.pos["usable_on_hand"] / r.d_mean
    elif atype in ("EXCESS", "OBSOLESCENCE", "AGING", "DEMAND_DROP"):
        imp["value_at_risk"] = max(r.excess_value * snap.cfg.holding_rate + r.obsolescence_exposure, r.obsolete_value)
        if ctx.get("eco_leftover_qty"):
            imp["value_at_risk"] = ctx["eco_leftover_qty"] * r.unit_cost
        if ctx.get("eol_exposure_value"):
            imp["value_at_risk"] = ctx["eol_exposure_value"]
        imp["inventory_impact"] = r.excess_value
    elif atype == "EXPIRY":
        imp["value_at_risk"] = (r.at_risk_qty + r.expired_qty) * r.unit_cost
        imp["inventory_impact"] = imp["value_at_risk"]
        tti = r.days_to_expiry if (r.days_to_expiry is not None and r.days_to_expiry >= 0) else 0.0
    elif atype == "LEAD_TIME_INCREASE":
        imp["value_at_risk"] = r.d_mean * ((r.lt_stats.get("mean") or 0) - (r.lt_static or 0)) * r.price
    elif atype == "QUALITY_HOLD":
        imp["value_at_risk"] = r.quarantined_value + r.blocked_value
    return imp, tti


def _delayed_inbound(inp) -> list[dict]:
    return [ib for ib in inp.inbound if (ib.get("delay_days") or 0) > 0 or (ib.get("promised_day") is not None and ib["promised_day"] < 0)]


def _dims_for_pair(snap, inp, r, atype) -> dict:
    d = {"sku": inp.sku, "node": inp.loc_code, "family": inp.item.get("family_code"), "industry": inp.item.get("industry")}
    if atype in ("STOCKOUT", "SAFETY_STOCK_BREACH", "ALLOCATION_CONFLICT"):
        bad = _delayed_inbound(inp)
        if bad:
            d["shipment"] = sorted({b["shipment_no"] for b in bad if b.get("shipment_no")})
            d["po"] = sorted({b["ref"] for b in bad if b.get("kind") == "PO"})
            d["supplier"] = sorted({snap.suppliers.get(b["supplier_id"], {}).get("code") for b in bad if b.get("supplier_id")} - {None})
            d["lane"] = sorted({b["lane"] for b in bad if b.get("lane")})
            d["port"] = sorted({b["port"] for b in bad if b.get("port")})
            d["reason"] = sorted({b["delay_reason"] for b in bad if b.get("delay_reason")})
        if inp.requirements:
            d["prod_order"] = sorted({q["ref"] for q in inp.requirements})[:5]
    elif inp.supplier_id and atype in ("LEAD_TIME_INCREASE",):
        d["supplier"] = [snap.suppliers.get(inp.supplier_id, {}).get("code")]
    return d


# ---------------------------------------------------------------------------------------------------------- detection
def run_detection(actor: str = "system") -> dict:
    snap = get_snapshot(force=True)
    weights = S.get("weights.priority")
    scorecards = risk_service.supplier_scorecards(snap)
    rules = [r for r in Rule.query.filter_by(enabled=True).all()]
    now = S.now()
    existing = {a.dedupe_key: a for a in Alert.query.filter(Alert.status.in_(STATUSES)).all()}
    seen: set[str] = set()
    created = updated = 0
    cands: list[dict] = []

    def emit(rule: Rule, key: str, ctx: dict, evidence: list, title: str, message: str, item_id=None, loc_id=None, sup_id=None, entity_ref=None,
             dims=None, impact=None, tti=None, root_cause=None):
        nonlocal created, updated
        seen.add(key)
        sev = severity_for(rule.severity, ctx)
        score, parts = priority_score(impact or {}, sev, tti, weights)
        a = existing.get(key)
        payload = dict(rule_code=rule.code, alert_type=rule.alert_type, severity=sev, item_id=item_id, location_id=loc_id, supplier_id=sup_id,
                       entity_ref=entity_ref, title=title, message=message, dims=dims or {}, metrics={e["metric"]: e["actual"] for e in evidence},
                       impact=impact or {}, recommended_action=rule.recommended_action, priority_score=score, priority_breakdown=parts,
                       time_to_impact_days=tti, root_cause=root_cause)
        if a is None:
            a = Alert(dedupe_key=key, status="New", owner=OWNERS.get(rule.alert_type, "Inventory Planner"), created_at=now, first_seen=now, last_seen=now, **payload)
            db.session.add(a)
            created += 1
        else:
            for k, v in payload.items():
                setattr(a, k, v)
            a.last_seen = now
            a.occurrences = (a.occurrences or 1) + 1
            if a.status == "Resolved":
                a.status = "New"
            updated += 1
        cands.append({"alert": a, "rule": rule})

    industries_active = {i for i in {inp.item.get("industry") for inp in snap.inputs.values()} if i}
    for rule in rules:
        applicable = rule.industries or None
        if rule.scope == "pair":
            for k, inp in snap.inputs.items():
                if applicable and inp.item.get("industry") not in applicable:
                    continue
                r = snap.results[k]
                ctx = pair_context(snap, inp, r)
                ok, ev = eval_condition(rule.condition, ctx)
                if not ok:
                    continue
                if rule.alert_type == "STOCKOUT" and ctx["demand_daily"] <= 0 and not inp.orders:
                    continue
                imp, tti = _pair_impact(snap, inp, r, rule.alert_type, ctx)
                dims = _dims_for_pair(snap, inp, r, rule.alert_type)
                title = f"{rule.name}: {inp.sku} @ {inp.loc_code}"
                msg = _message(rule, inp, r, ctx)
                rc = None
                if dims.get("reason"):
                    rc = "; ".join(dims["reason"])
                emit(rule, f"{rule.code}|{inp.sku}|{inp.loc_code}", ctx, ev, title, msg, inp.item_id, inp.location_id, inp.supplier_id, None, dims, imp, tti, rc)
        elif rule.scope == "supply":
            for k, inp in snap.inputs.items():
                if applicable and inp.item.get("industry") not in applicable:
                    continue
                r = snap.results[k]
                for ib in inp.inbound:
                    if ib.get("kind") not in ("PO", "TO"):
                        continue
                    prom = ib.get("promised_day")
                    ctx = {"kind": ib["kind"], "overdue_days": (-prom if prom is not None and prom < 0 else 0),
                           "eta_slip_days": (ib["eta_day"] - prom) if prom is not None else 0, "qty": ib["qty"], "value": ib["qty"] * r.unit_cost}
                    ok, ev = eval_condition(rule.condition, ctx)
                    if not ok:
                        continue
                    sup = snap.suppliers.get(ib.get("supplier_id"), {})
                    dims = {"po": [ib["ref"]] if ib["kind"] == "PO" else [], "shipment": [ib["shipment_no"]] if ib.get("shipment_no") else [],
                            "supplier": [sup.get("code")] if sup else [], "lane": [ib["lane"]] if ib.get("lane") else [], "port": [ib["port"]] if ib.get("port") else [],
                            "sku": inp.sku, "node": inp.loc_code, "reason": [ib["delay_reason"]] if ib.get("delay_reason") else []}
                    dest_risk = r.lost_sales_value + r.production_risk
                    imp = {"value_at_risk": ctx["value"], "revenue_at_risk": dest_risk, "production_at_risk": r.production_risk, "service_impact": r.service_impact,
                           "customers": 0, "contract_customers": 0, "critical": inp.item.get("criticality") == "Critical", "inventory_impact": ctx["value"]}
                    tti = r.days_to_stockout if r.days_to_stockout is not None else (ib["eta_day"] if ib["eta_day"] >= 0 else 0)
                    what = f"{ib['kind']} {ib['ref']} for {inp.sku} @ {inp.loc_code}: promised day {prom:+d}, ETA day {ib['eta_day']:+d} ({ctx['eta_slip_days']:.0f} d slip)" if prom is not None else f"{ib['ref']}"
                    emit(rule, f"{rule.code}|{ib['ref']}|{inp.sku}|{inp.loc_code}", ctx, ev, f"{rule.name}: {ib['ref']} ({inp.sku})", what, inp.item_id, inp.location_id,
                         ib.get("supplier_id"), ib["ref"], dims, imp, tti, ib.get("delay_reason"))
        elif rule.scope == "supplier":
            for sid, sc in scorecards.items():
                if sc["n_obs"] < 5:
                    continue
                ctx = {"otif": sc["otif"], "lt_reliability": sc["on_time"], "risk_score": sc["risk_score"]}
                ok, ev = eval_condition(rule.condition, ctx)
                if ok:
                    imp = {"value_at_risk": sc["open_value"] * 0.25, "revenue_at_risk": 0.0, "service_impact": 0.0, "customers": 0, "contract_customers": 0,
                           "critical": bool(sc["critical_skus"]) and sc["risk_score"] > 60}
                    emit(rule, f"{rule.code}|{sc['code']}", ctx, ev, f"{rule.name}: {sc['name']}",
                         f"OTIF {sc['otif']:.0%}, on-time {sc['on_time']:.0%}, lead-time CV {sc['lt_cv'] or 0:.2f}; risk score {sc['risk_score']:.0f}/100",
                         None, None, sid, sc["code"], {"supplier": [sc["code"]]}, imp, None, "Supplier performance deterioration")
        elif rule.scope == "location":
            for lid, l in snap.locs.items():
                cap = l.get("capacity_units") or 0
                if not cap:
                    continue
                oh = sum(snap.results[k].pos["on_hand"] for k in snap.keys_for_loc(lid))
                ctx = {"utilization": oh / cap}
                ok, ev = eval_condition(rule.condition, ctx)
                if ok:
                    emit(rule, f"{rule.code}|{l['code']}", ctx, ev, f"{rule.name}: {l['code']}", f"Utilisation {oh / cap:.0%} ({oh:,.0f} of {cap:,.0f} units)",
                         None, lid, None, l["code"], {"node": l["code"]}, {"value_at_risk": max(oh - cap, 0) * 20, "customers": 0, "contract_customers": 0, "service_impact": 0.0},
                         None, None)
        elif rule.scope == "lot":
            lots = {l.id: l for l in Lot.query.filter(Lot.quality_status.in_(["QUARANTINE", "BLOCKED", "REJECTED"])).all()}
            for k, inp in snap.inputs.items():
                for lot in inp.lots:
                    L = lots.get(lot["lot_id"])
                    if not L or lot["qty"] <= 0:
                        continue
                    ctx = {"quality_status": "BLOCKED" if L.quality_status == "REJECTED" else L.quality_status,
                           "hold_days": (snap.today - (L.received_date or snap.today)).days}
                    ok, ev = eval_condition(rule.condition, ctx)
                    if ok:
                        emit(rule, f"{rule.code}|{L.lot_no}|{inp.loc_code}", ctx, ev, f"{rule.name}: lot {L.lot_no}", f"{lot['qty']:,.0f} units held ({L.quality_status}) for {ctx['hold_days']} days at {inp.loc_code}",
                             inp.item_id, inp.location_id, None, L.lot_no, {"sku": inp.sku, "node": inp.loc_code, "lot": [L.lot_no]},
                             {"value_at_risk": lot["qty"] * snap.results[k].unit_cost, "customers": 0, "contract_customers": 0, "service_impact": 0.0}, None, None)
        elif rule.scope == "recon":
            rec = reconciliation_service.reconcile(snap)
            for row in rec["rows"]:
                unit_cost = snap.items.get(row["item_id"], {}).get("unit_cost") or 0
                ctx = {"variance_pct_abs": row["variance_pct_abs"], "recon_status": row["status"], "variance_value": abs(row["variance"] or (row["ict_qty"] or 0)) * unit_cost}
                ok, ev = eval_condition(rule.condition, ctx)
                if ok:
                    val = abs(row["variance"] or 0) * (snap.items.get(row["item_id"], {}).get("unit_cost") or 0)
                    emit(rule, f"{rule.code}|{row['system']}|{row['sku']}|{row['location']}", ctx, ev,
                         f"{rule.name}: {row['sku']} @ {row['location']} ({row['system']})",
                         f"{row['system']} {row['source_qty']:,.0f} vs ICT {row['ict_qty'] if row['ict_qty'] is not None else 'n/a'} - {row['status']}. {row['note']}",
                         row["item_id"], row["location_id"], None, row["system"], {"sku": row["sku"], "node": row["location"], "system": row["system"]},
                         {"value_at_risk": val, "customers": 0, "contract_customers": 0, "service_impact": 0.0}, None, None)
        elif rule.scope == "sync":
            for row in reconciliation_service.latency_report(snap):
                if row["ratio"] is None:
                    continue
                ctx = {"age_ratio": row["ratio"]}
                ok, ev = eval_condition(rule.condition, ctx)
                if ok:
                    emit(rule, f"{rule.code}|{row['source']}", ctx, ev, f"{rule.name}: {row['source']}",
                         f"Last sync {row['age_hours']:.0f} h ago (expected every {row['expected_hours']:.0f} h)", None, None, None, row["source"],
                         {"system": row["source"]}, {"value_at_risk": 0, "customers": 0, "contract_customers": 0, "service_impact": 0.0}, None, "Stale integration feed")
    db.session.flush()

    # ---- auto-resolve alerts whose condition cleared
    resolved = 0
    for key, a in existing.items():
        if key not in seen and a.status in ("New", "Acknowledged", "Investigating"):
            a.status = "Resolved"
            a.impact = {**(a.impact or {}), "resolved_reason": "condition cleared on latest detection"}
            resolved += 1
    # ---- suppression & escalation
    sup_rules = S.get("alerts.suppress") or []
    min_pri = S.get("alerts.min_priority")
    min_pri = 25.0 if min_pri is None else min_pri
    suppressed = escalated = 0
    for c in cands:
        a, rule = c["alert"], c["rule"]
        a.suppressed, a.suppressed_reason = False, None
        for sr in sup_rules:
            if (sr.get("rule") in (None, "*", a.rule_code)) and (not sr.get("sku") or sr["sku"] == (a.dims or {}).get("sku")) and \
               (not sr.get("until") or sr["until"] >= now.date().isoformat()):
                a.suppressed, a.suppressed_reason = True, f"suppression rule: {sr.get('reason', 'muted')}"
        if not a.suppressed and a.priority_score < min_pri and a.severity == "LOW":
            a.suppressed, a.suppressed_reason = True, f"low priority (< {min_pri:g}) and LOW severity"
        if rule.suppress_hours and a.status == "Dismissed" and a.last_seen and (now - a.last_seen) < timedelta(hours=rule.suppress_hours):
            a.suppressed, a.suppressed_reason = True, "recently dismissed"
        if a.status in ("New", "Acknowledged") and a.severity in ("HIGH", "CRITICAL") and rule.escalate_after_hours and a.first_seen and \
           (now - a.first_seen) > timedelta(hours=rule.escalate_after_hours) and not a.escalated:
            a.escalated = True
            a.owner = "Supply Chain Manager"
            escalated += 1
    db.session.flush()
    for a in Alert.query.filter(Alert.status.in_(STATUSES)).all():
        if not a.alert_no:
            a.alert_no = f"ALR-{a.id:06d}"
    n_inc = incident_service.cluster(snap)
    n_risk = risk_service.build_risk_register(snap, scorecards)
    from . import recommendation_service
    n_rec = recommendation_service.generate(snap)
    S.set_value("system.last_detection", now.isoformat(), audit=False)
    audit.log("DATA", "Alert", "*", "detection_run", {"created": created, "updated": updated, "resolved": resolved, "suppressed": suppressed, "escalated": escalated,
                                                      "incidents": n_inc, "risks": n_risk, "recommendations": n_rec}, actor=actor)
    db.session.commit()
    active = Alert.query.filter(Alert.status.in_(OPEN_STATUSES), Alert.suppressed.is_(False)).count()
    return {"created": created, "updated": updated, "resolved": resolved, "escalated": escalated, "incidents": n_inc, "risks": n_risk,
            "recommendations": n_rec, "active_alerts": active, "suppressed": Alert.query.filter_by(suppressed=True).count()}


def _message(rule: Rule, inp, r, ctx) -> str:
    t = rule.alert_type
    if t == "STOCKOUT":
        d = f"first shortfall {r.stockout_date:%d %b}" if r.stockout_date else "no shortfall in the deterministic projection"
        return (f"Stock-out probability {r.stockout_prob:.0%} over the {r.lt_plan:.0f}-day lead time; usable {r.pos['usable_on_hand']:,.0f}, "
                f"projected minimum {r.min_projected:,.0f}, safety stock {r.ss:,.0f}; {d}.")
    if t == "SAFETY_STOCK_BREACH":
        return f"Usable stock {r.pos['usable_on_hand']:,.0f} is below safety stock {r.ss:,.0f}."
    if t == "EXCESS":
        return f"{r.excess_qty:,.0f} units above max level/{ctx['inv_class']} - excess value {r.excess_value:,.0f}."
    if t == "OBSOLESCENCE":
        if ctx.get("eco_leftover_qty"):
            return f"{ctx['eco_leftover_qty']:,.0f} units of the superseded revision cannot be consumed before the ECO cut-over."
        if ctx.get("eol_exposure_value"):
            return f"Stock + inbound exceed forecast consumption until end-of-life: exposure {ctx['eol_exposure_value']:,.0f}."
        return f"Inventory class {r.inv_class}: {r.on_hand_value:,.0f} on hand with no recent demand."
    if t == "EXPIRY":
        return f"Expiry status {r.expiry_status}; {r.at_risk_qty:,.0f} units at risk of expiring before consumption, {r.expired_qty:,.0f} already expired."
    if t == "LEAD_TIME_INCREASE":
        return f"Observed mean lead time {r.lt_stats['mean']:.0f} d vs master {r.lt_static:.0f} d over {r.lt_stats['n']} receipts (P90 {r.lt_stats['p90']:.0f} d)."
    if t in ("DEMAND_SPIKE", "DEMAND_DROP"):
        return f"Last 4 weeks are {ctx['demand_ratio_4w']:.1f}× the prior mean (z = {ctx['demand_z']:.1f})."
    if t == "ALLOCATION_CONFLICT":
        return f"Claims exceed usable stock (available {r.pos['available']:,.0f})." if ctx["over_allocated"] else "Due demand exceeds available stock plus timely inbound."
    if t == "AGING":
        return f"Oldest stock is {ctx['oldest_days']:.0f} days old."
    return rule.description or rule.name


# ---------------------------------------------------------------------------------------------------------- lifecycle
def set_status(alert_id: int, status: str, actor: str, note: str | None = None) -> Alert:
    if status not in STATUSES:
        raise ValueError(f"Unknown status '{status}'")
    a = Alert.query.get(alert_id)
    if not a:
        raise ValueError("Alert not found")
    old = a.status
    a.status = status
    audit.log("ACTION", "Alert", a.id, "alert_status", {"from": old, "to": status, "note": note}, actor=actor)
    return a


def explain(a: Alert) -> dict:
    """Answer the eight root-cause questions for an alert."""
    imp = a.impact or {}
    dims = a.dims or {}
    return {
        "what": a.title, "detail": a.message,
        "why": a.root_cause or "; ".join(dims.get("reason", [])) or _why_from_metrics(a),
        "when": f"Detected {a.first_seen:%d %b %H:%M}" + (f"; impact expected in ~{a.time_to_impact_days:.0f} days" if a.time_to_impact_days is not None else ""),
        "where": ", ".join(x for x in [dims.get("node") and f"node {dims['node']}", dims.get("lane") and f"lane {', '.join(dims['lane'])}",
                                        dims.get("port") and f"port {', '.join(dims['port'])}"] if x) or "n/a",
        "who": ", ".join(x for x in [f"SKU {dims.get('sku')}" if dims.get("sku") else None,
                                      f"supplier {', '.join(dims['supplier'])}" if dims.get("supplier") else None,
                                      f"POs {', '.join(dims['po'])}" if dims.get("po") else None,
                                      f"{imp.get('customers', 0)} customer(s)" if imp.get("customers") else None] if x) or "n/a",
        "how_big": {"value_at_risk": imp.get("value_at_risk"), "revenue_at_risk": imp.get("revenue_at_risk"), "production_at_risk": imp.get("production_at_risk"),
                    "service_impact": imp.get("service_impact")},
        "options": a.recommended_action,
        "if_nothing": "See recommendation options: expected loss is the revenue/production at risk above." if imp.get("revenue_at_risk") or imp.get("production_at_risk")
        else "Exposure grows with time (carrying cost / obsolescence / expiry) until the condition is addressed.",
    }


def _why_from_metrics(a: Alert) -> str:
    m = a.metrics or {}
    bits = []
    if "stockout_prob" in m and m["stockout_prob"] is not None:
        bits.append(f"probability {m['stockout_prob']:.0%} of demand exceeding supply within the lead time")
    if "projected_min" in m and m["projected_min"] is not None:
        bits.append(f"projected minimum {m['projected_min']:,.0f}")
    return "; ".join(bits) or "rule condition met (see evidence)"
