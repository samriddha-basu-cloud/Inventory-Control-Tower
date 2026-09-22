"""Replenishment recommendations, decision options, safety stock methods, lead-time intelligence."""
from __future__ import annotations

from collections import defaultdict

from flask import Blueprint, redirect, render_template, request, url_for

from ..extensions import db
from ..models import ReplenishmentPolicy, SafetyStockPolicy
from ..services import (action_service, audit_service, lead_time_service, optimization_service, replenishment_service, safety_stock_service, settings_service as S)
from ..services.engine import bootstrap_ltd
from ..utils import charts
from ..utils.security import require
from . import helpers as H

bp = Blueprint("replenishment", __name__)


@bp.route("/replenishment")
def home():
    if not H.has_data():
        return render_template("replenishment/home.html", empty=True)
    snap, f = H.snap(), H.flt()
    only = request.args.get("only") == "1"
    rows = []
    for i, r in snap.rows(f):
        rc = r.rec
        if only and not rc["qty"]:
            continue
        rows.append({"sku": i.sku, "node": i.loc_code, "policy": rc["policy_name"], "position": r.pos["position"], "rop": r.rop, "ss": r.ss, "eoq": r.eoq, "q": rc["qty"], "raw": rc["raw_qty"],
                     "order_date": rc["order_date"], "arrival": rc["arrival_date"], "post": rc["post_order_position"], "risk_no": rc["risk_if_not_ordered"]["probability"], "risk": r.risk_level,
                     "cost": rc["cost"], "notes": "; ".join(rc["notes"]) or ("⚠ " + "; ".join(rc["violations"]) if rc["violations"] else ""), "trig": "Yes" if rc["triggered"] else "",
                     "expl": rc["explanation"]})
    rows.sort(key=lambda x: (-x["risk_no"], x["sku"]))
    pols = [{"level": p.scope_level, "key": p.scope_key, "params": p.params, "notes": p.notes} for p in ReplenishmentPolicy.query.order_by(ReplenishmentPolicy.id).all()]
    return render_template("replenishment/home.html", empty=False, rows=rows, only=only, policies=pols, catalog=replenishment_service.POLICIES, trig=sum(1 for r in rows if r["trig"]),
                           total_cost=sum(r["cost"] for r in rows), levels=["GLOBAL", "INDUSTRY", "REGION", "NODE", "CATEGORY", "SKU", "SKU_LOCATION"])


@bp.route("/replenishment/decision")
def decision():
    snap = H.snap()
    sku, loc = request.args.get("sku"), request.args.get("loc")
    key = next((k for k, i in snap.inputs.items() if i.sku == sku and i.loc_code == loc), None)
    if not key:
        return render_template("replenishment/decision.html", res=None, pairs=[(i.sku, i.loc_code) for i in snap.inputs.values()][:500])
    inp, r = snap.inputs[key], snap.results[key]
    res = optimization_service.generate_options(snap, key)
    expl = optimization_service.decision_explainer(inp, r)
    return render_template("replenishment/decision.html", res=res, inp=inp, r=r, expl=expl, sku=sku, loc=loc, key=key, pairs=[])


@bp.route("/replenishment/act", methods=["POST"])
@require("create_action")
def act():
    snap = H.snap()
    sku, loc, idx = request.form.get("sku"), request.form.get("loc"), int(request.form.get("option", 0))
    key = next((k for k, i in snap.inputs.items() if i.sku == sku and i.loc_code == loc), None)
    if not key:
        H.err("Unknown SKU/location.")
        return H.back("replenishment.home")
    res = optimization_service.generate_options(snap, key)
    if idx >= len(res["options"]) or res["options"][idx]["type"] == "DO_NOTHING":
        H.err("Choose an actionable option.")
        return H.back("replenishment.home")
    o = res["options"][idx]
    r = snap.results[key]
    try:
        a = action_service.create_action(o["type"], item_id=key[0], location_id=key[1], qty=o["qty"], supplier_id=o.get("supplier_id"), cost=o["incremental_cost"], why=f"{o['label']} - {snap.inputs[key].sku} @ {loc}",
                                         payload={"arrival_days": o.get("arrival_days"), "ref": o.get("ref"), "from_location_id": o.get("from_location_id"), "option": o["label"]},
                                         alternatives=[{k: x.get(k) for k in ("type", "label", "qty", "incremental_cost", "arrival_days", "feasible", "violations")} for x in res["options"]],
                                         created_by=H.actor())
        db.session.commit()
        H.ok(f"Action {a.action_no} created → {a.status.replace('_', ' ').title()}. See the Action Center.")
        return redirect(url_for("actions.action_detail", action_id=a.id))
    except action_service.ActionError as e:
        db.session.rollback()
        H.err(str(e))
        return H.back("replenishment.home")


@bp.route("/replenishment/policy", methods=["POST"])
@require("configure")
def add_policy():
    import json
    try:
        params = json.loads(request.form.get("params") or "{}")
        pol = str(params.get("policy", "")).upper()
        if pol not in replenishment_service.POLICIES:
            raise ValueError(f"policy must be one of {', '.join(replenishment_service.POLICIES)}")
        level = request.form.get("level", "SKU")
        if level not in ("GLOBAL", "INDUSTRY", "REGION", "NODE", "CATEGORY", "SKU", "SKU_LOCATION"):
            raise ValueError("Invalid scope level")
        p = ReplenishmentPolicy(scope_level=level, scope_key=(request.form.get("key") or "*")[:120], params=params, notes=(request.form.get("notes") or "")[:250], source_system="UI")
        db.session.add(p)
        audit_service.log("CONFIG", "ReplenishmentPolicy", f"{level}:{p.scope_key}", "policy_added", {"params": params}, actor=H.actor())
        S.bump_version()
        db.session.commit()
        H.ok("Replenishment policy saved; the engine re-evaluates every affected item-location immediately.")
    except (ValueError, json.JSONDecodeError) as e:
        db.session.rollback()
        H.err(f"Not saved: {e}")
    return H.back("replenishment.home")


@bp.route("/safety-stock")
def safety_stock():
    if not H.has_data():
        return render_template("replenishment/safety_stock.html", empty=True)
    snap, f = H.snap(), H.flt()
    sku, loc = request.args.get("sku"), request.args.get("loc")
    keys = snap.keys(f)
    key = next((k for k in snap.inputs if snap.inputs[k].sku == sku and snap.inputs[k].loc_code == loc), None) or (keys[0] if keys else None)
    detail = None
    if key:
        inp, r = snap.inputs[key], snap.results[key]
        st = lead_time_service.compute_stats(inp.lt_obs, inp.static_lt, snap.cfg.min_lt_obs)
        L_bases = {}
        for basis in ("static", "observed_mean", "p50", "p90"):
            L, sL, note = lead_time_service.planning_lead_time(st, basis, True, snap.cfg.default_lt_days)
            L_bases[basis] = (L, sL)
        methods = []
        R = float(inp.repl.get("review_days") or snap.cfg.review_days)
        for m in safety_stock_service.METHODS:
            L, sL = L_bases.get(snap.cfg.lt_basis, (r.lt_plan, r.lt_sigma))
            ltd = bootstrap_ltd(inp.hist_weekly[-52:], L) if m == "empirical" else None
            try:
                res = safety_stock_service.safety_stock(m, d_mean=r.d_mean, d_std=r.d_std, L=L, L_std=sL, service_level=r.service_level, review_days=R, order_qty=r.practical_q or r.eoq, ltd_samples=ltd)
                methods.append({"method": m, "name": safety_stock_service.METHODS[m]["name"], "formula": res.formula, "ss": res.ss, "rop": r.d_mean * L + res.ss, "z": res.z, "use": safety_stock_service.METHODS[m]["use"],
                                "chosen": "◀ in use" if m == r.ss_method else ""})
            except ValueError as e:
                methods.append({"method": m, "name": m, "formula": str(e), "ss": None, "rop": None, "z": None, "use": "", "chosen": ""})
        basis_rows = [{"basis": b, "L": v[0], "sigma": v[1], "ss": safety_stock_service.safety_stock("combined", d_mean=r.d_mean, d_std=r.d_std, L=v[0], L_std=v[1], service_level=r.service_level).ss} for b, v in L_bases.items()]
        detail = {"inp": inp, "r": r, "methods": methods, "basis_rows": basis_rows, "st": st}
    rows = [{"sku": i.sku, "node": i.loc_code, "method": r.ss_method, "sl": r.service_level, "z": r.z, "d": r.d_mean, "sd": r.d_std, "L": r.lt_plan, "sL": r.lt_sigma, "ss": r.ss, "rop": r.rop, "note": r.ss_method_note,
             "profile": r.demand_profile} for i, r in snap.rows(f)]
    return render_template("replenishment/safety_stock.html", empty=False, detail=detail, rows=rows, methods_doc=safety_stock_service.METHODS, basis=snap.cfg.lt_basis, use_obs=snap.cfg.use_observed_lt,
                           levels=["GLOBAL", "INDUSTRY", "REGION", "NODE", "CATEGORY", "SKU", "SKU_LOCATION"])


@bp.route("/safety-stock/policy", methods=["POST"])
@require("configure")
def add_ss_policy():
    method = request.form.get("method") or None
    try:
        sl = H.fnum(request.form.get("service_level"))
        if method and method not in safety_stock_service.METHODS:
            raise ValueError("Unknown method")
        if sl is not None and not (0.5 <= sl < 1):
            raise ValueError("Service level must be between 0.5 and 0.9999")
        params = {k: v for k, v in {"method": method, "service_level": sl}.items() if v is not None}
        if not params:
            raise ValueError("Choose a method and/or a service level")
        level = request.form.get("level", "SKU")
        p = SafetyStockPolicy(scope_level=level, scope_key=(request.form.get("key") or "*")[:120], params=params, notes="Set in Safety Stock page", source_system="UI")
        db.session.add(p)
        audit_service.log("CONFIG", "SafetyStockPolicy", f"{level}:{p.scope_key}", "policy_added", params, actor=H.actor())
        S.bump_version()
        db.session.commit()
        H.ok("Safety-stock policy saved and applied.")
    except ValueError as e:
        db.session.rollback()
        H.err(f"Not saved: {e}")
    return H.back("replenishment.safety_stock")


@bp.route("/lead-time")
def lead_time():
    snap, f = H.snap(), H.flt()
    groups = defaultdict(lambda: {"obs": [], "static": [], "keys": 0})
    for k in snap.keys(f):
        inp = snap.inputs[k]
        r = snap.results[k]
        label = snap.suppliers.get(inp.supplier_id, {}).get("code") or f"{snap.locs.get(inp.source_loc_id, {}).get('code', 'internal')}→"
        gk = (label, inp.loc_code if not inp.supplier_id else "")
        g = groups[gk]
        g["obs"] += inp.lt_obs if not g["obs"] or inp.supplier_id is None else []
        if inp.static_lt:
            g["static"].append(inp.static_lt)
        g["keys"] += 1
    rows = []
    for (label, node), g in groups.items():
        st = lead_time_service.compute_stats(g["obs"], (sum(g["static"]) / len(g["static"])) if g["static"] else None, snap.cfg.min_lt_obs)
        if not st.n:
            continue
        rows.append({"lane": label + (f" → {node}" if node else ""), "n": st.n, "static": st.static, "mean": st.mean, "median": st.median, "std": st.std, "p50": st.p50, "p75": st.p75, "p90": st.p90, "p95": st.p95,
                     "gap": (st.p90 - st.static) if (st.p90 and st.static) else None, "dist": st.dist["type"] if st.dist else "empirical only", "ks": st.dist["ks_p"] if st.dist else None,
                     "eligible": "Yes" if st.eligible else "No", "elig": "RECOMMENDED" if st.eligible else "NOT_RECOMMENDED"})
    rows.sort(key=lambda r: -(r["gap"] or 0))
    fig = charts.grouped_bars([r["lane"] for r in rows[:14]], [{"name": "Static", "y": [r["static"] for r in rows[:14]]}, {"name": "Observed P50", "y": [r["p50"] for r in rows[:14]]}, {"name": "Observed P90", "y": [r["p90"] for r in rows[:14]]}],
                              height=320, ytitle="Days")
    return render_template("replenishment/lead_time.html", rows=rows, fig=fig, basis=snap.cfg.lt_basis, use_obs=snap.cfg.use_observed_lt, min_obs=snap.cfg.min_lt_obs)
