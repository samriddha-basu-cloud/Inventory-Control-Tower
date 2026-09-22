"""Risk center, supply visibility, order pegging & allocation."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from flask import Blueprint, redirect, render_template, request, url_for

from ..extensions import db
from ..models import Allocation, ControlPolicy, Peg, PurchaseOrder, Risk, SalesOrder, Shipment
from ..rules import policies as pol
from ..services import allocation_service, audit_service, risk_service, settings_service as S
from ..utils import charts
from ..utils.security import require
from . import helpers as H

bp = Blueprint("risk", __name__)


@bp.route("/risk")
def center():
    if not H.has_data():
        return render_template("risk/center.html", empty=True)
    snap, f = H.snap(), H.flt()
    by = request.args.get("by", "location")
    if by not in ("sku", "location", "supplier", "family", "region"):
        by = "location"
    heat = risk_service.heatmap(snap, by, f)
    rows_h = sorted(heat["rows"], key=lambda r: -max(r["values"]))[:40]
    hrefs = None
    url_for_by = {"sku": "/inventory/sku/{}", "location": "/inventory/location/{}", "supplier": "/inventory/supplier/{}"}.get(by)
    hm = charts.heatmap(heat["columns"], [r["key"] for r in rows_h], [r["values"] for r in rows_h])
    if url_for_by:
        hm["data"][0]["customdata"] = [[url_for_by.format(r["key"])] * len(heat["columns"]) for r in rows_h]
    risks = Risk.query.order_by(Risk.exposure.desc()).limit(400).all()
    rrows = [{"id": r.risk_id, "type": r.risk_type, "sku": r.item.sku if r.item else "", "node": r.location.code if r.location else "", "supplier": r.supplier.code if r.supplier else "",
              "cause": r.cause, "p": r.probability, "impact": r.impact_value, "exposure": r.exposure, "sev": r.severity, "action": r.recommended_action, "owner": r.owner, "status": r.status} for r in risks]
    by_type = defaultdict(float)
    for r in risks:
        by_type[r.risk_type.replace("_", " ").title()] += r.exposure
    tfig = charts.bar_chart(list(by_type), list(by_type.values()), height=300, horizontal=True, ytitle="Exposure (probability × impact)")
    stock = [{"sku": i.sku, "node": i.loc_code, "usable": r.pos["usable_on_hand"], "d": r.d_mean, "lt": r.lt_plan, "p": r.stockout_prob, "risk": r.risk_level, "days": r.days_to_stockout, "short": r.exp_short, "lost": r.lost_sales_value,
             "date": r.stockout_date, "lt_static": r.lt_static, "lt_p90": r.lt_p90} for i, r in snap.rows(f) if r.d_mean > 0 and r.risk_level != "LOW"]
    stock.sort(key=lambda x: -x["p"])
    th = S.get("thresholds.stockout")
    return render_template("risk/center.html", empty=False, hm=hm, by=by, rrows=rrows, tfig=tfig, stock=stock, th=th, total_exposure=sum(r.exposure for r in risks),
                           sc=sorted(risk_service.supplier_scorecards(snap).values(), key=lambda s: -s["risk_score"]), weights=S.get("weights.supplier_risk"), use_obs=snap.cfg.use_observed_lt, basis=snap.cfg.lt_basis)


@bp.route("/supply")
def supply():
    snap, f = H.snap(), H.flt()
    q = (request.args.get("q") or "").lower()
    rows = []
    for k in snap.keys(f):
        inp, r = snap.inputs[k], snap.results[k]
        for ib in inp.inbound:
            eta = snap.today + timedelta(days=ib["eta_day"])
            prom = (snap.today + timedelta(days=ib["promised_day"])) if ib.get("promised_day") is not None else None
            delay = (ib["eta_day"] - ib["promised_day"]) if ib.get("promised_day") is not None else 0
            row = {"kind": ib["kind"], "ref": ib["ref"], "shipment": ib.get("shipment_no") or "", "sku": inp.sku, "node": inp.loc_code, "supplier": snap.suppliers.get(ib.get("supplier_id"), {}).get("code", ""),
                   "qty": ib["qty"], "promised": prom, "eta": eta, "actual": None, "status": "IN TRANSIT" if ib.get("in_transit") else ("PRODUCTION" if ib["kind"] == "PROD" else "ON ORDER"),
                   "delay": delay, "reason": ib.get("delay_reason") or "", "lane": ib.get("lane") or "", "risk": r.risk_level,
                   "short_date": r.stockout_date, "short_qty": r.expected_shortage_qty}
            if not q or q in " ".join(str(v).lower() for v in row.values()):
                rows.append(row)
    rows.sort(key=lambda x: (-(x["delay"] or 0), x["eta"]))
    ships = Shipment.query.filter(Shipment.status != "DELIVERED").order_by(Shipment.eta_date).all()
    srows = [{"no": s.shipment_no, "ref": s.ref_number, "supplier": s.supplier.code if s.supplier else "", "dest": s.dest.code if s.dest else "", "lane": s.lane, "port": s.port, "mode": s.mode, "dispatch": s.dispatch_date,
              "promised": s.promised_date, "eta": s.eta_date, "delay": s.delay_days, "status": s.status, "reason": s.delay_reason, "lines": ", ".join(l.item.sku for l in s.lines), "exp": "Yes" if s.expedited else ""}
             for s in ships if not q or q in (s.shipment_no + (s.ref_number or "") + (s.lane or "")).lower()]
    shortages = [{"sku": i.sku, "node": i.loc_code, "first": r.stockout_date, "day": r.first_stockout_day, "qty": r.expected_shortage_qty, "lost": r.expected_shortage_qty * r.price, "pess": r.pessimistic_stockout_day,
                  "risk": r.risk_level, "inbound": r.pos["in_transit"] + r.pos["on_order"]} for i, r in snap.rows(f) if r.first_stockout_day is not None and r.first_stockout_day <= 45]
    shortages.sort(key=lambda x: x["day"])
    delayed = [r for r in rows if (r["delay"] or 0) > 0]
    return render_template("risk/supply.html", rows=rows, srows=srows, shortages=shortages, q=q, delayed=len(delayed), in_transit=sum(1 for r in rows if r["status"] == "IN TRANSIT"))


@bp.route("/pegging")
def pegging():
    snap = H.snap()
    q = (request.args.get("q") or "").lower()
    items = {i["id"]: i["sku"] for i in snap.items.values()}
    locs = {l["id"]: l["code"] for l in snap.locs.values()}
    pegs = Peg.query.order_by(Peg.location_id, Peg.demand_date).all()
    demand_totals = defaultdict(float)
    peg_by_demand = defaultdict(float)
    prow = []
    for p in pegs:
        peg_by_demand[(p.item_id, p.location_id, p.demand_ref)] += p.qty
        row = {"sku": items.get(p.item_id), "node": locs.get(p.location_id), "demand": f"{p.demand_type} {p.demand_ref}", "supply": f"{p.supply_type} {p.supply_ref}", "qty": p.qty, "expected": p.expected_date,
               "due": p.demand_date, "late": "Late" if p.expected_date and p.demand_date and p.expected_date > p.demand_date else "On time", "customer": snap.customers.get(p.customer_id, {}).get("code", "")}
        if not q or q in " ".join(str(v).lower() for v in row.values()):
            prow.append(row)
    unmet = []
    for k, inp in snap.inputs.items():
        for o in inp.orders + inp.extra.get("backorders", []) + [{"ref": x["ref"], "qty": x["qty"], "due_day": x["due_day"], "customer_id": None} for x in inp.requirements]:
            got = peg_by_demand.get((k[0], k[1], o["ref"]), 0.0)
            if o["qty"] - got > 0.5:
                unmet.append({"sku": inp.sku, "node": inp.loc_code, "demand": o["ref"], "customer": snap.customers.get(o.get("customer_id"), {}).get("code", ""), "qty": o["qty"], "allocated": got, "remaining": o["qty"] - got,
                              "due": snap.today + timedelta(days=o["due_day"]), "note": "past due" if o["due_day"] < 0 else ""})
    if q:
        unmet = [u for u in unmet if q in " ".join(str(v).lower() for v in u.values())]
    allocs = Allocation.query.filter_by(status="ACTIVE").count()
    policies = [{"level": p.scope_level, "key": p.scope_key, "params": p.params} for p in ControlPolicy.query.filter_by(policy_type="allocation").all()]
    # scarcity simulator
    sim = None
    sku, node = request.args.get("sku"), request.args.get("node")
    if sku and node:
        key = next((k for k, i in snap.inputs.items() if i.sku == sku and i.loc_code == node), None)
        if key:
            inp = snap.inputs[key]
            avail = H.fnum(request.args.get("supply"), inp.stock.get("UNRESTRICTED", 0.0))
            demands = [{"id": o["ref"], "qty": o["qty"], "score": 0.0, **o} for o in inp.orders + inp.extra.get("backorders", [])]
            sim = {"key": key, "supply": avail, "arms": {}}
            for method in ("fifo", "priority", "revenue", "contract", "weighted"):
                ranked = allocation_service.rank_demands([dict(d) for d in demands], inp.item, inp.loc, {"method": method}, snap.customers)
                left, alloc = avail, {}
                for d in ranked:
                    take = min(left, d["qty"])
                    alloc[d["ref"]] = take
                    left -= take
                sim["arms"][method] = {"alloc": alloc, "unmet": sum(d["qty"] for d in ranked) - sum(alloc.values()), "value": sum(alloc[d["ref"]] * (d.get("value", 0) / d["qty"] if d["qty"] else 0) for d in ranked)}
            lp = allocation_service.scarcity_plan(avail, [{"id": d["ref"], "qty": d["qty"], "score": max(0.05, (5 - d.get("customer_priority", 3)) / 4)} for d in demands], H.fnum(request.args.get("minfill"), 0.0) / 100.0)
            sim["lp"] = lp
            sim["demands"] = demands
    return render_template("risk/pegging.html", prow=prow, unmet=unmet, allocs=allocs, policies=policies, sim=sim, q=q, methods=allocation_service.METHODS, weights=allocation_service.DEFAULT_WEIGHTS,
                           skus=sorted({i.sku for i in snap.inputs.values() if i.orders}), n_pegs=len(pegs))


@bp.route("/pegging/run", methods=["POST"])
@require("create_action")
def run_pegging():
    res = allocation_service.run_pegging_all()
    db.session.commit()
    H.ok(f"Pegging re-run: {res['pegs']} pegs, {res['allocations']} stock allocations, {res['unmet_demands']} demands with unmet quantity.")
    return H.back("risk.pegging")


@bp.route("/pegging/allocate", methods=["POST"])
@require("create_action")
def allocate():
    snap = H.snap()
    sku, node, qty = request.form.get("sku"), request.form.get("node"), H.fnum(request.form.get("qty"))
    key = next((k for k, i in snap.inputs.items() if i.sku == sku and i.loc_code == node), None)
    if not key or not qty:
        H.err("Missing SKU / node / quantity.")
        return H.back("risk.pegging")
    try:
        allocation_service.commit_allocation(key[0], key[1], qty, demand_ref=request.form.get("ref") or None, alloc_type=request.form.get("type", "COMMITTED"), actor=H.actor())
        db.session.commit()
        H.ok(f"Allocated {qty:,.0f} of {sku} at {node}.")
    except allocation_service.AllocationError as e:
        db.session.rollback()
        H.err(str(e))
    return H.back("risk.pegging")
