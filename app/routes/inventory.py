"""Inventory visibility: explorer, SKU/Location/Supplier 360, health, segmentation, aging, expiry/FEFO, ledger, traceability, reconciliation."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from flask import Blueprint, abort, render_template, request

from ..extensions import db
from ..models import (Alert, Allocation, AuditLog, Customer, Forecast, InventoryTransaction, Item, LeadTimeObservation, Location, Lot, Peg, ProductionOrder, PurchaseOrder,
                      Risk, SalesOrder, SalesOrderLine, Scenario, Shipment, Supplier, TransferOrder)
from ..services import (abc_xyz_service, audit_service, expiry_service, health_service, inventory_service, kpi_service, ledger_service, optimization_service, reconciliation_service,
                        report_service, risk_service, settings_service as S, traceability_service)
from ..services import forecast_service
from ..utils import charts
from ..utils.security import require
from . import helpers as H

bp = Blueprint("inventory", __name__)


def _pair_row(snap, inp, r, seg):
    return {"sku": inp.sku, "desc": inp.item.get("description"), "node": inp.loc_code, "on_hand": r.pos["on_hand"], "available": r.pos["available"], "allocated": r.pos["allocated"],
            "in_transit": r.pos["in_transit"], "on_order": r.pos["on_order"], "position": r.pos["position"], "ss": r.ss, "days_supply": r.days_supply, "demand": r.d_mean,
            "risk": r.risk_level, "value": r.on_hand_value, "abc": seg.get("abc"), "xyz": seg.get("xyz"), "age": r.age_days, "expiry": r.days_to_expiry, "cls": r.inv_class,
            "industry": inp.item.get("industry")}


@bp.route("/inventory")
def explorer():
    if not H.has_data():
        return render_template("inventory/explorer.html", empty=True, rows=[])
    snap, f = H.snap(), H.flt()
    rows = [_pair_row(snap, i, r, snap.item_seg.get(i.item_id, {})) for i, r in snap.rows(f)]
    return render_template("inventory/explorer.html", empty=False, rows=rows, tips=inventory_service.POSITION_TOOLTIPS,
                           totals=snap.totals(f))


@bp.route("/inventory/health")
def health():
    snap, f = H.snap(), H.flt()
    kp = kpi_service.compute_all(snap, f)
    hi = health_service.compute(snap, f, kp)
    sections = {"Availability": ["stockout_rate", "days_of_supply", "stockout_risk"], "Efficiency": ["inventory_turns", "dio", "excess_pct", "slow_pct"],
                "Risk": ["obsolete_pct", "lt_variability", "lt_reliability", "supplier_otif"], "Financial": ["carrying_cost", "working_capital", "excess_capital", "lost_sales_exposure"],
                "Service": ["service_level", "fill_rate", "otif", "backorder_rate", "order_cycle_time"], "Quality": ["inventory_accuracy", "forecast_bias"]}
    from ..services import financial_service
    carb = financial_service.carbon_overview(snap)
    return render_template("inventory/health.html", kp=kp, hi=hi, sections=sections, carb=carb, measure_docs=kpi_service.MEASURE_DOCS)


@bp.route("/inventory/abc-xyz")
def abc_xyz():
    snap = H.snap()
    combine = [c for c in (request.args.get("combine") or "abc,xyz").split(",") if c in ("abc", "xyz", "fsn", "hml", "ved", "sde", "criticality")] or ["abc", "xyz"]
    rows = []
    for iid, sg in snap.item_seg.items():
        it = snap.items[iid]
        combo = "-".join(str(sg.get(c) if c != "criticality" else (it.get("criticality") or "?")[:1]) for c in combine)
        rows.append({"sku": it["sku"], "desc": it["description"], "annual": sg["annual_demand"], "cost": it["unit_cost"], "acv": sg["annual_value"], "cum": sg["cum_pct"], "abc": sg["abc"],
                     "cv": None if sg["cv"] == float("inf") else sg["cv"], "xyz": sg["xyz"], "fsn": sg["fsn"], "hml": sg["hml"], "ved": it.get("ved"), "sde": it.get("sde"),
                     "crit": it.get("criticality"), "combo": combo, "turns": sg["turns"]})
    rows.sort(key=lambda r: -r["acv"])
    m = abc_xyz_service.matrix([{"abc": r["abc"], "xyz": r["xyz"], "annual_value": r["acv"]} for r in rows])
    z = [[m[a][x]["count"] for x in "XYZ"] for a in "ABC"]
    mx = max(max(r) for r in z) or 1
    hm = charts.heatmap(["X (stable)", "Y (variable)", "Z (erratic)"], ["A", "B", "C"], [[v / mx for v in r] for r in z], height=250,
                        text=[[f"{m[a][x]['count']} SKUs · {m[a][x]['value'] / max(sum(r['acv'] for r in rows), 1):.0%} value" for x in "XYZ"] for a in "ABC"])
    par = charts.pareto([r["sku"] for r in rows[:60]], [r["acv"] for r in rows[:60]], [r["abc"] for r in rows[:60]], height=300)
    combos = defaultdict(int)
    for r in rows:
        combos[r["combo"]] += 1
    return render_template("inventory/abc_xyz.html", rows=rows, hm=hm, par=par, combine=combine, combos=sorted(combos.items(), key=lambda kv: -kv[1]),
                           th={"abc": S.get("abc.thresholds"), "xyz": S.get("xyz.thresholds"), "fsn": S.get("fsn.thresholds"), "hml": S.get("hml.thresholds")})


@bp.route("/inventory/aging")
def aging():
    snap, f = H.snap(), H.flt()
    by = request.args.get("by", "sku")
    if by not in ("sku", "location", "batch", "lot", "supplier", "category"):
        by = "sku"
    t = report_service.aging_table(snap, by, f)
    labels = t.columns[4:]
    top = t.rows[:15]
    fig = charts.grouped_bars([r[0] for r in top], [{"name": lb, "y": [r[4 + i] for r in top], "color": f"@seq{min(i + 2, 7)}"} for i, lb in enumerate(labels)], stack=True, height=340, ytitle="Value by age bucket")
    rows = [dict(zip(["key", "qty", "value", "avg_age"] + [f"b{i}" for i in range(len(labels))], r)) for r in t.rows]
    cols = [{"k": "key", "l": by.upper()}, {"k": "qty", "l": "Qty", "t": "num"}, {"k": "value", "l": "Value", "t": "money"}, {"k": "avg_age", "l": "Avg age (d)", "t": "num"}] + \
           [{"k": f"b{i}", "l": lb + " d", "t": "money"} for i, lb in enumerate(labels)]
    return render_template("inventory/aging.html", by=by, cols=cols, rows=rows, fig=fig, buckets=S.get("aging.buckets"))


@bp.route("/inventory/expiry")
def expiry():
    snap, f = H.snap(), H.flt()
    t = report_service.expiry_table(snap, f)
    rows = [dict(zip(["sku", "node", "lot", "qty", "state", "quality", "expiry", "dte", "pct", "status", "value"], r)) for r in t.rows]
    counts = defaultdict(float)
    for r in rows:
        counts[r["status"]] += r["value"]
    fig = charts.bar_chart(list(counts), list(counts.values()), height=260, ytitle="Value", colors=[charts.STATUS_COLOR.get({"OK": "NORMAL", "NEAR": "WATCH", "CRITICAL": "CRITICAL", "EXPIRED": "CRITICAL"}.get(k, "LOW"), "@s1") for k in counts])
    pick = None
    sku, node, qty = request.args.get("sku"), request.args.get("node"), H.fnum(request.args.get("qty"))
    if sku and node and qty:
        inp = next((i for i in snap.inputs.values() if i.sku == sku and i.loc_code == node), None)
        if inp:
            pick = expiry_service.fefo_pick(inp.lots, qty, snap.today, require_released=True, min_remaining_days=int(H.fnum(request.args.get("min_days"), 0)))
    return render_template("inventory/expiry.html", rows=rows, fig=fig, pick=pick, q={"sku": sku, "node": node, "qty": qty}, th=snap.cfg.expiry_thresholds)


@bp.route("/inventory/ledger", methods=["GET", "POST"])
def ledger():
    snap = H.snap()
    sku, node = request.values.get("sku"), request.values.get("node")
    rep = None
    err_msg = None
    if request.method == "POST":
        from ..utils.security import has_perm
        if not has_perm("ingest"):
            abort(403)
        try:
            it, lc = Item.query.filter_by(sku=request.form["sku"]).first(), Location.query.filter_by(code=request.form["node"]).first()
            if not it or not lc:
                raise ledger_service.LedgerError("Missing SKU or location.")
            to = Location.query.filter_by(code=request.form.get("to_node") or "").first()
            lot = Lot.query.filter_by(item_id=it.id, lot_no=request.form.get("lot") or "").first() if request.form.get("lot") else None
            ledger_service.post(request.form["type"], it.id, lc.id, float(request.form["qty"]), lot_id=lot.id if lot else None, to_location_id=to.id if to else None,
                                uom=request.form.get("uom") or None, direction=request.form.get("direction") or None, reason=request.form.get("reason") or "Manual posting",
                                txn_id=request.form.get("txn_id") or None, actor=H.actor())
            db.session.commit()
            H.ok("Transaction posted. History is append-only; correct mistakes with a reversal.")
        except (ledger_service.LedgerError, ValueError, KeyError) as e:
            db.session.rollback()
            H.err(str(e))
        except Exception as e:  # unit conversion etc.
            db.session.rollback()
            H.err(str(e))
    if sku and node:
        it, lc = Item.query.filter_by(sku=sku).first(), Location.query.filter_by(code=node).first()
        if it and lc:
            start = H.fdate(request.args.get("from"))
            rep = ledger_service.ledger_report(it.id, lc.id, start, H.fdate(request.args.get("to")))
    recent = InventoryTransaction.query.order_by(InventoryTransaction.id.desc()).limit(40).all() if not rep else []
    return render_template("inventory/ledger.html", rep=rep, sku=sku, node=node, recent=recent, types=[t for t in ledger_service.TXN_TYPES if t not in ("OPENING",)],
                           skus=sorted(i.sku for i in Item.query.all())[:500], nodes=sorted(l.code for l in Location.query.all()))


@bp.route("/inventory/ledger/reverse/<int:txn_id>", methods=["POST"])
@require("ingest")
def reverse_txn(txn_id):
    t = InventoryTransaction.query.get_or_404(txn_id)
    try:
        ledger_service.reverse(t, request.form.get("reason") or "manual reversal", actor=H.actor())
        db.session.commit()
        H.ok(f"Reversal posted for transaction {t.id}; the original remains in the ledger.")
    except (ledger_service.LedgerError, ValueError) as e:
        db.session.rollback()
        H.err(str(e))
    return H.back("inventory.ledger")


@bp.route("/inventory/trace")
def trace():
    q = request.args.get("lot", "").strip()
    tr = None
    if q:
        lot = traceability_service.find_lot(q)
        tr = traceability_service.trace(lot) if lot else None
        if not lot:
            H.err(f"No lot/batch matching '{q}'.")
    sample = [l.lot_no for l in Lot.query.filter(Lot.lot_no.like("TRACE%")).all()]
    return render_template("inventory/trace.html", q=q, tr=tr, sample=sample)


@bp.route("/inventory/reconciliation")
def reconciliation():
    snap = H.snap()
    rec = reconciliation_service.reconcile(snap, [request.args["system"]] if request.args.get("system") else None)
    lat = reconciliation_service.latency_report(snap)
    st = request.args.get("status")
    rows = [r for r in rec["rows"] if not st or r["status"] == st]
    fig = charts.bar_chart(list(rec["summary"]), list(rec["summary"].values()), height=240, ytitle="Records", horizontal=True)
    return render_template("inventory/reconciliation.html", rec=rec, rows=rows, lat=lat, fig=fig, sel_status=st, sel_system=request.args.get("system"))


# ------------------------------------------------------------------------------------------------------ 360 views
@bp.route("/inventory/sku/<sku>")
def sku360(sku):
    snap = H.snap()
    it = Item.query.filter_by(sku=sku).first_or_404()
    keys = snap.keys_for_item(it.id)
    if not keys:
        return render_template("inventory/sku360.html", it=it, keys=[], nodes=[])
    seg = snap.item_seg.get(it.id, {})
    nodes = [_pair_row(snap, snap.inputs[k], snap.results[k], seg) for k in keys]
    loc_code = request.args.get("loc")
    sel = next((k for k in keys if snap.inputs[k].loc_code == loc_code), None) or max(keys, key=lambda k: snap.results[k].stockout_prob)
    inp, r = snap.inputs[sel], snap.results[sel]
    proj = charts.projection_chart(r.projection["rows"], r.ss, height=320, labels=[(snap.today + timedelta(days=x["start_day"])).strftime("%d %b") for x in r.projection["rows"]])
    lt = r.lt_stats
    lt_fig = charts.histogram_lt(inp.lt_obs, lt.get("static"), lt.get("p50"), lt.get("p90")) if inp.lt_obs else None
    # demand history + forecast (aggregate over nodes) ------------------------------------------------------------------
    hw = inp.extra.get("hist_weeks", [])
    fc_x = [snap.today - timedelta(days=snap.today.weekday()) + timedelta(weeks=i) for i in range(len(inp.forecast_weekly))]
    fcs = defaultdict(dict)
    for fr in Forecast.query.filter_by(item_id=it.id, location_id=sel[1]).filter(Forecast.period_start >= fc_x[0] if fc_x else True).all():
        fcs[fr.forecast_type][fr.period_start] = fr.qty
    series = [{"name": "Actual demand", "y": inp.hist_weekly, "color": "@s1"}]
    x_hist = [d.isoformat() for d in hw]
    dfig = {"data": [{"type": "scatter", "mode": "lines", "x": x_hist, "y": inp.hist_weekly, "name": "Actual demand", "line": {"color": "@s1", "width": 2}}]
            + [{"type": "scatter", "mode": "lines+markers", "x": [d.isoformat() for d in sorted(v)], "y": [v[d] for d in sorted(v)], "name": f"{t.title()} forecast",
                "line": {"color": ["@s2", "@s3", "@s7"][i % 3], "width": 2, "dash": "dot"}} for i, (t, v) in enumerate(sorted(fcs.items()))],
            "layout": {"height": 300, "margin": {"l": 50, "r": 10, "t": 10, "b": 40}, "legend": {"orientation": "h", "y": -0.2}, "yaxis": {"title": "Units / week", "rangemode": "tozero"}}}
    explainer = optimization_service.decision_explainer(inp, r)
    orders = {
        "po": PurchaseOrder.query.join(PurchaseOrder.lines).filter(PurchaseOrder.lines.any(item_id=it.id), PurchaseOrder.status.in_(["OPEN", "PARTIAL"])).all(),
        "so": SalesOrderLine.query.filter_by(item_id=it.id).join(SalesOrder).filter(SalesOrder.status.in_(["OPEN", "PARTIAL"])).all(),
        "to": TransferOrder.query.filter_by(item_id=it.id).filter(TransferOrder.status.in_(["OPEN", "IN_TRANSIT", "PLANNED"])).all(),
        "mo": ProductionOrder.query.filter_by(item_id=it.id).filter(ProductionOrder.status.in_(["OPEN", "RELEASED"])).all(),
    }
    lots = []
    for k in keys:
        for l in snap.inputs[k].lots:
            if l["qty"] > 0:
                info = expiry_service.expiry_info(l.get("expiry"), None, it.shelf_life_days, snap.today, snap.cfg.expiry_thresholds)
                lots.append({"node": snap.inputs[k].loc_code, "lot": l.get("lot_no") or "(none)", "qty": l["qty"], "state": l["state"], "quality": l.get("quality"), "received": l.get("received"),
                             "expiry": l.get("expiry"), "dte": info["days_to_expiry"], "status": info["status"], "age": (snap.today - l["received"]).days if l.get("received") else None})
    pegs = Peg.query.filter_by(item_id=it.id).order_by(Peg.location_id, Peg.demand_date).limit(200).all()
    allocs = Allocation.query.filter_by(item_id=it.id, status="ACTIVE").all()
    alerts = Alert.query.filter_by(item_id=it.id).filter(Alert.status.notin_(["Resolved", "Dismissed"])).order_by(Alert.priority_score.desc()).all()
    risks = Risk.query.filter_by(item_id=it.id).order_by(Risk.exposure.desc()).all()
    audit = AuditLog.query.filter_by(entity_id=sku).order_by(AuditLog.id.desc()).limit(25).all()
    scen = [s for s in Scenario.query.order_by(Scenario.id.desc()).limit(20).all() if sku in str(s.changes) or (s.results and any(x.get("sku") == sku for x in s.results.get("scenario", {}).get("by_pair", [])))]
    hi = health_service.sku_health(snap, it.id)
    sup = snap.suppliers.get(inp.supplier_id)
    sc = risk_service.supplier_scorecards(snap).get(inp.supplier_id) if inp.supplier_id else None
    chain = forecast_service.chain(sel, float(request.args.get("fc", 10))) if inp.forecast_weekly else None
    return render_template("inventory/sku360.html", it=it, keys=keys, nodes=nodes, sel=inp, r=r, seg=seg, proj=proj, lt_fig=lt_fig, dfig=dfig, explainer=explainer, orders=orders, lots=lots,
                           pegs=pegs, allocs=allocs, alerts=alerts, risks=risks, audit=audit, scen=scen, hi=hi, sup=sup, sc=sc, chain=chain, fc_pct=float(request.args.get("fc", 10)),
                           tips=inventory_service.POSITION_TOOLTIPS, locs={l.id: l.code for l in Location.query.all()}, custs={c.id: c.name for c in Customer.query.all()})


@bp.route("/inventory/sku/<sku>/drawer")
def sku_drawer(sku):
    snap = H.snap()
    it = Item.query.filter_by(sku=sku).first_or_404()
    keys = snap.keys_for_item(it.id)
    rows = [_pair_row(snap, snap.inputs[k], snap.results[k], snap.item_seg.get(it.id, {})) for k in keys]
    return render_template("inventory/_sku_drawer.html", it=it, rows=rows)


@bp.route("/inventory/location/<code>")
def location360(code):
    snap = H.snap()
    loc = Location.query.filter_by(code=code).first_or_404()
    keys = snap.keys_for_loc(loc.id)
    rows = [_pair_row(snap, snap.inputs[k], snap.results[k], snap.item_seg.get(k[0], {})) for k in keys]
    res = [snap.results[k] for k in keys]
    inp = [snap.inputs[k] for k in keys]
    on_hand = sum(r.pos["on_hand"] for r in res)
    cap = loc.capacity_units or 0
    inbound = [{"kind": ib["kind"], "ref": ib["ref"], "sku": i.sku, "qty": ib["qty"], "eta": snap.today + timedelta(days=ib["eta_day"]), "promised": (snap.today + timedelta(days=ib["promised_day"])) if ib.get("promised_day") is not None else None,
                "delay": ib.get("delay_days"), "transit": ib.get("in_transit")} for i in inp for ib in i.inbound]
    outbound = [{"ref": t["ref"], "sku": i.sku, "qty": t["qty"], "day": t["day"]} for i in inp for t in i.transfers_out]
    sups = defaultdict(lambda: {"skus": 0, "inbound": 0.0})
    for i in inp:
        if i.supplier_id:
            s = sups[snap.suppliers[i.supplier_id]["code"]]
            s["skus"] += 1
            s["inbound"] += sum(b["qty"] for b in i.inbound)
    custs = defaultdict(lambda: {"orders": 0, "units": 0.0, "value": 0.0})
    for i in inp:
        for o in i.orders + i.extra.get("backorders", []):
            c = custs[snap.customers.get(o.get("customer_id"), {}).get("code", "?")]
            c["orders"] += 1
            c["units"] += o["qty"]
            c["value"] += o.get("value", 0)
    from ..services import report_service as rs
    aging = rs.aging_table(snap, "location", H.PairFilter(location=code))
    ag_fig = charts.bar_chart(aging.columns[4:], aging.rows[0][4:] if aging.rows else [], height=240, ytitle="Value") if aging.rows else None
    stockouts = [r for r in res if r.pos["usable_on_hand"] <= 0 and r.d_mean > 0]
    fill = 1.0
    lines = SalesOrderLine.query.join(SalesOrder).filter(SalesOrder.ship_from_location_id == loc.id, SalesOrder.status == "SHIPPED").all()
    if lines:
        fill = sum(l.qty_shipped for l in lines) / max(sum(l.qty_ordered for l in lines), 1)
    heat = risk_service.heatmap(snap, "sku", H.PairFilter(location=code))
    hm = charts.heatmap(heat["columns"], [r["key"] for r in heat["rows"]][:20], [r["values"] for r in heat["rows"]][:20]) if heat["rows"] else None
    return render_template("inventory/location360.html", loc=loc, rows=rows, on_hand=on_hand, cap=cap, util=(on_hand / cap) if cap else None, inbound=inbound, outbound=outbound,
                           sups=sorted(sups.items()), custs=sorted(custs.items(), key=lambda kv: -kv[1]["value"]), ag_fig=ag_fig, stockouts=stockouts, fill=fill, hm=hm,
                           excess_value=sum(r.excess_value for r in res), value=sum(r.on_hand_value for r in res),
                           alerts=Alert.query.filter_by(location_id=loc.id).filter(Alert.status.notin_(["Resolved", "Dismissed"])).order_by(Alert.priority_score.desc()).limit(25).all(),
                           risks=Risk.query.filter_by(location_id=loc.id).order_by(Risk.exposure.desc()).limit(25).all(), n_pairs=len(keys))


@bp.route("/inventory/supplier/<code>")
def supplier360(code):
    snap = H.snap()
    sup = Supplier.query.filter_by(code=code).first_or_404()
    sc = risk_service.supplier_scorecards(snap).get(sup.id)
    obs = LeadTimeObservation.query.filter_by(supplier_id=sup.id).order_by(LeadTimeObservation.received_date).all()
    static = [inp.static_lt for inp in snap.inputs.values() if inp.supplier_id == sup.id and inp.static_lt]
    lts = [o.lead_time_days for o in obs]
    lt_fig = charts.histogram_lt(lts, sum(static) / len(static) if static else None, sc["lt_p90"] and sorted(lts)[len(lts) // 2] if lts else None, sc["lt_p90"]) if lts else None
    # monthly OTIF history
    by_month = defaultdict(lambda: [0, 0])
    for o in obs:
        if o.received_date:
            k = o.received_date.strftime("%Y-%m")
            by_month[k][0] += 1
            by_month[k][1] += 1 if (o.lead_time_days <= (o.promised_days or o.lead_time_days) + 1 and (o.qty_received or 0) >= 0.95 * (o.qty_ordered or 0)) else 0
    months = sorted(by_month)[-12:]
    hist_fig = charts.line_chart(months, [{"name": "OTIF %", "y": [100 * by_month[m][1] / by_month[m][0] for m in months]}], height=260, ytitle="%") if months else None
    comp_fig = charts.bar_chart([k.replace("_", " ") for k in sc["components"]], [v * 100 * sc["weights"].get(k, 0) / (sum(sc["weights"].values()) or 1) for k, v in sc["components"].items()], height=240,
                                ytitle="Points of risk score", horizontal=True) if sc else None
    pos = PurchaseOrder.query.filter_by(supplier_id=sup.id).filter(PurchaseOrder.status.in_(["OPEN", "PARTIAL"])).all()
    po_rows = [{"po": p.po_number, "dest": p.dest.code if p.dest else None, "sku": ", ".join(l.item.sku for l in p.lines), "qty": sum(l.qty_ordered - (l.qty_received or 0) for l in p.lines),
                "promised": p.promised_date, "eta": p.eta_date, "delay": (p.eta_date - p.promised_date).days if p.eta_date and p.promised_date else 0, "reason": p.delay_reason,
                "value": sum((l.qty_ordered - (l.qty_received or 0)) * (l.unit_price or 0) for l in p.lines)} for p in pos]
    skus = [{"sku": i.sku, "desc": i.description, "crit": i.criticality, "node": inp.loc_code, "risk": snap.results[k].risk_level} for k, inp in snap.inputs.items() if inp.supplier_id == sup.id
            for i in [Item.query.get(inp.item_id)] if i.criticality in ("Critical", "High")]
    delays = [s for s in Shipment.query.filter_by(supplier_id=sup.id).filter(Shipment.status.in_(["DELAYED", "IN_TRANSIT"])).all()]
    spend = sum(r["value"] for r in po_rows)
    return render_template("inventory/supplier360.html", sup=sup, sc=sc, lt_fig=lt_fig, hist_fig=hist_fig, comp_fig=comp_fig, po_rows=po_rows, skus=skus, delays=delays, spend=spend,
                           alerts=Alert.query.filter_by(supplier_id=sup.id).filter(Alert.status.notin_(["Resolved", "Dismissed"])).order_by(Alert.priority_score.desc()).limit(20).all(),
                           risks=Risk.query.filter_by(supplier_id=sup.id).order_by(Risk.exposure.desc()).limit(20).all())
