"""Control Tower home, session filters, demo loading, auth, misc endpoints."""
from __future__ import annotations

import os
from collections import defaultdict
from datetime import timedelta

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash

from ..extensions import db
from ..models import Alert, Incident, KpiSnapshot, Recommendation, Role, User
from ..services import (alert_service, demo_service, financial_service, health_service, industry_service, jobs, kpi_service, network_service, risk_service,
                        settings_service as S)
from ..services.demo_specs import INDUSTRY_LABELS
from ..services.snapshot import PairFilter
from ..utils import charts
from ..utils.security import require
from . import helpers as H

bp = Blueprint("main", __name__)


@bp.route("/")
def home():
    if not H.has_data():
        return render_template("control/home.html", empty=True, industries=INDUSTRY_LABELS)
    snap, f = H.snap(), H.flt()
    tot = snap.totals(f)
    kp = kpi_service.compute_all(snap, f)
    health = health_service.compute(snap, f, kp)
    fin = financial_service.overview(snap, f)
    carb = financial_service.carbon_overview(snap)
    net = network_service.build(snap, f)
    # inventory state cards ------------------------------------------------------------------------------------
    states = [("Total inventory value", tot.get("on_hand_value", 0) + tot.get("in_transit_value", 0) + tot.get("on_order_value", 0) * 0, "on-hand + in-transit value (on-order shown separately)"),
              ("On-hand", tot.get("on_hand_value", 0), "physical stock, all counted states"), ("Available", tot.get("available_value", 0), "usable − allocated − committed − reserved"),
              ("Allocated", tot.get("allocated_value", 0), "claims for open orders"), ("Committed / reserved", tot.get("committed_value", 0), "hard commitments & reservations"),
              ("In-transit", tot.get("in_transit_value", 0), "shipped, not received"), ("On-order", tot.get("on_order_value", 0), "ordered, not shipped"),
              ("WIP", tot.get("wip_value", 0), "on the shop floor"), ("Quarantined", tot.get("quarantined_value", 0), "held for quality"), ("Blocked", tot.get("blocked_value", 0), "blocked stock"),
              ("Excess", tot.get("excess_value", 0), "beyond policy max & DOS threshold"), ("Obsolete", tot.get("obsolete_value", 0), "flagged obsolete"), ("At-risk (expiry)", tot.get("at_risk_value", 0), "will expire before consumption")]
    # position by location (stacked) ---------------------------------------------------------------------------
    by_loc = defaultdict(lambda: defaultdict(float))
    for inp, r in snap.rows(f):
        b = by_loc[inp.loc_code]
        b["Available"] += max(r.pos["available"], 0) * r.unit_cost
        b["Allocated/committed"] += (r.pos["allocated"] + r.pos["committed"] + r.pos["reserved"]) * r.unit_cost
        b["Non-usable (quarantine/blocked/other)"] += r.pos["non_usable"] * r.unit_cost
        b["In-transit + on-order"] += (r.pos["in_transit"] + r.pos["on_order"]) * r.unit_cost
    top = sorted(by_loc.items(), key=lambda kv: -sum(kv[1].values()))[:14]
    pos_fig = charts.grouped_bars([k for k, _ in top], [{"name": n, "y": [v[n] for _, v in top]} for n in ("Available", "Allocated/committed", "Non-usable (quarantine/blocked/other)", "In-transit + on-order")],
                                  height=360, stack=True, ytitle="Value", bottom=110)
    heat = risk_service.heatmap(snap, "location", f)
    hm = charts.heatmap(heat["columns"], [r["key"] for r in heat["rows"]][:18], [r["values"] for r in heat["rows"]][:18], height=None)
    net_fig = charts.network_graph(net["nodes"], net["edges"], height=420)
    incidents = Incident.query.filter(Incident.status.notin_(["Resolved", "Dismissed"])).order_by(Incident.priority_score.desc()).limit(6).all()
    recs = Recommendation.query.filter_by(status="OPEN").order_by(Recommendation.priority_score.desc()).limit(6).all()
    # trend ---------------------------------------------------------------------------------------------------------
    def series(metric):
        pts = KpiSnapshot.query.filter_by(metric=metric).order_by(KpiSnapshot.as_of).all()
        return [p.as_of.isoformat() for p in pts], [p.value for p in pts]
    tx, ty = series("inventory_value")
    sx, sy = series("service_level")
    trend = charts.line_chart(tx, [{"name": "Inventory value", "y": ty}], height=260, ytitle="Value", fill_first=True) if tx else None
    svc_trend = charts.line_chart(sx, [{"name": "Service level (line fill)", "y": [v * 100 for v in sy]}], height=260, ytitle="%") if sx else None
    fin_fig = charts.bar_chart(["Carrying cost /yr", "Excess capital", "Obsolescence exposure", "Markdown exposure", "Lost sales (exp.)", "Expedite cost"],
                               [fin["carrying_cost_year"], fin["excess_capital"], fin["obsolescence_exposure"], fin["markdown_exposure"], fin["lost_sales"], fin["expedite_cost"]],
                               height=260, horizontal=True, colors=["@s1", "@s2", "@s4", "@s5", "@s7", "@s8"])
    modes = list(carb["by_mode"].items())
    carb_fig = charts.bar_chart([m for m, _ in modes] or ["none"], [v for _, v in modes] or [0], height=260, ytitle="kg CO2e (estimate)", color="@s3")
    fatigue = {"raw": Alert.query.filter(Alert.status.in_(alert_service.OPEN_STATUSES)).count(), "suppressed": Alert.query.filter_by(suppressed=True).count(),
               "clustered": Alert.query.filter(Alert.incident_id.isnot(None), Alert.status.in_(alert_service.OPEN_STATUSES)).count(), "incidents": Incident.query.filter(Incident.status.notin_(["Resolved", "Dismissed"])).count()}
    fatigue["to_review"] = max(0, fatigue["raw"] - fatigue["suppressed"] - fatigue["clustered"]) + fatigue["incidents"]
    groups = {"Availability": ["stockout_rate", "stockout_risk", "days_of_supply"], "Efficiency": ["inventory_turns", "dio", "excess_pct", "slow_pct"],
              "Risk": ["obsolete_pct", "lt_variability", "lt_reliability"], "Financial": ["carrying_cost", "working_capital", "excess_capital", "lost_sales_exposure"],
              "Service": ["service_level", "fill_rate", "otif", "backorder_rate", "order_cycle_time", "supplier_otif"], "Quality": ["inventory_accuracy", "forecast_bias"]}
    return render_template("control/home.html", empty=False, tot=tot, kp=kp, health=health, fin=fin, carb=carb, states=states, pos_fig=pos_fig, hm=hm, net_fig=net_fig,
                           incidents=incidents, recs=recs, trend=trend, svc_trend=svc_trend, fin_fig=fin_fig, carb_fig=carb_fig, fatigue=fatigue, groups=groups,
                           last_detection=S.get("system.last_detection"), demo=S.get("system.demo_loaded"))


@bp.route("/plotly.min.js")
def plotly_js():
    """Serve the Plotly bundle shipped inside the `plotly` Python package: works offline, no CDN dependency."""
    import plotly
    path = os.path.join(os.path.dirname(plotly.__file__), "package_data", "plotly.min.js")
    resp = send_file(path, mimetype="application/javascript", max_age=86400)
    return resp


@bp.route("/filters", methods=["POST"])
def set_filters():
    if request.form.get("reset"):
        session.pop("filters", None)
    else:
        session["filters"] = {k: (request.form.get(k) or "").strip()[:80] for k in PairFilter.KEYS if request.form.get(k)}
    return H.back()


@bp.route("/demo/load", methods=["POST"])
@require("admin")
def load_demo():
    sel = request.form.getlist("industries") or list(INDUSTRY_LABELS)
    out = jobs.submit("load_demo", demo_service.load_demo, industries=sel, seed=int(request.form.get("seed") or 42))
    if out.status == "DONE":
        r = out.result or {}
        H.ok(f"Demo network loaded: {r.get('items', 0)} SKUs, {r.get('locations', 0)} nodes, {r.get('pos', 0)} POs, "
             f"{(r.get('detection') or {}).get('active_alerts', 0)} alerts grouped into {(r.get('detection') or {}).get('incidents', 0)} incidents.")
    else:
        H.err(f"Demo load failed: {out.error}")
    session.pop("filters", None)
    return redirect(url_for("main.home"))


@bp.route("/detect", methods=["POST"])
@require("alert_manage")
def detect():
    job = jobs.submit("run_detection", alert_service.run_detection, actor=H.actor())
    if job.status == "DONE":
        H.ok(f"Detection complete: {job.result.get('created', 0)} new, {job.result.get('updated', 0)} updated, {job.result.get('resolved', 0)} auto-resolved, {job.result.get('incidents', 0)} incidents.")
    else:
        H.err(f"Detection failed: {job.error}")
    return H.back()


@bp.route("/jobs/<job_id>")
def job_status(job_id):
    j = jobs.get(job_id)
    if not j:
        abort(404)
    return jsonify({"job_id": j.job_id, "kind": j.kind, "status": j.status, "result": j.result, "error": j.error})


@bp.route("/switch-role", methods=["POST"])
def switch_role():
    if current_app.config.get("AUTH_REQUIRED"):
        abort(403, "Role switching is disabled when authentication is required.")
    r = request.form.get("role")
    if Role.query.filter_by(name=r).first():
        session["role"] = r
    return H.back()


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = User.query.filter_by(username=(request.form.get("username") or "").strip(), active=True).first()
        if u and u.password_hash and check_password_hash(u.password_hash, request.form.get("password") or ""):
            session.clear()
            session["username"], session["role"] = u.username, u.role
            return H.back()
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.login") if current_app.config.get("AUTH_REQUIRED") else url_for("main.home"))


@bp.route("/health")
def health():
    from ..models import Item
    try:
        n = Item.query.count()
        return jsonify({"status": "ok", "items": n, "as_of": S.today().isoformat()})
    except Exception as e:  # pragma: no cover
        return jsonify({"status": "error", "detail": str(e)[:120]}), 500
