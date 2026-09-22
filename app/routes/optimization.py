"""Optimization (MILP replenishment) and rebalancing."""
from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, url_for

from ..extensions import db
from ..services import action_service, experiment_service, jobs, optimization_service as opt, settings_service as S
from ..utils.security import require
from . import helpers as H

bp = Blueprint("optimization", __name__)
TERMS = ["stockout", "holding", "expedite", "carbon", "working_capital", "obsolescence"]
TERM_LABEL = {"stockout": "Stock-outs (lost margin)", "expedite": "Expedite / freight", "holding": "Holding cost", "working_capital": "Working capital", "obsolescence": "Obsolescence",
              "carbon": "Carbon (priced)", "ordering_freight": "Ordering + truck freight"}


def _weights_from(form) -> dict:
    w = S.get("optimization.weights")
    for t in TERMS:
        v = H.fnum(form.get(f"w_{t}"))
        if v is not None and v >= 0:
            w[t] = v
    return w


def _solve(**kw):
    snap = H.snap()
    res = opt.replenishment_plan(snap, kw.pop("f", None), **kw)
    return res


@bp.route("/optimization", methods=["GET", "POST"])
def home():
    if not H.has_data():
        return render_template("optimization/home.html", empty=True)
    snap = H.snap()
    res = None
    params = {"budget": S.get("optimization.budget"), "truck_kg": S.get("optimization.truck_capacity_kg"), "warehouse_m3": S.get("optimization.warehouse_capacity_m3"), "service_floor": 0.9,
              "enforce": S.get("optimization.enforce_service_level"), "weights": S.get("optimization.weights")}
    if request.method == "POST":
        w = _weights_from(request.form)
        params = {"budget": H.fnum(request.form.get("budget")), "truck_kg": H.fnum(request.form.get("truck_kg")), "warehouse_m3": H.fnum(request.form.get("warehouse_m3")),
                  "service_floor": (H.fnum(request.form.get("service_floor"), 90) or 90) / 100.0, "enforce": bool(request.form.get("enforce")), "weights": w}
        try:
            job = jobs.submit("optimize_replenishment", _job_replenish, budget=params["budget"], truck_kg=params["truck_kg"], warehouse_m3=params["warehouse_m3"], service_floor=params["service_floor"],
                              enforce=params["enforce"], weights=w)
            res = job.result if job.status == "DONE" else {"status": "FAILED", "explanation": [job.error or "Optimisation failed"], "plan": [], "objective": [], "constraints": []}
        except Exception as e:  # never leak stack traces
            res = {"status": "FAILED", "explanation": [str(e)[:200]], "plan": [], "objective": [], "constraints": []}
    return render_template("optimization/home.html", empty=False, res=res, params=params, eligibility=experiment_service.eligibility(snap), term_label=TERM_LABEL, terms=TERMS)


def _job_replenish(budget, truck_kg, warehouse_m3, service_floor, enforce, weights):
    r = opt.replenishment_plan(H.snap(), None, budget=budget, truck_kg=truck_kg, warehouse_m3=warehouse_m3, service_floor=service_floor, weights=weights, enforce_service=enforce)
    r["plan"] = [{k: v for k, v in p.items()} for p in r.get("plan", [])]
    return r


@bp.route("/optimization/create-actions", methods=["POST"])
@require("create_action")
def create_actions():
    snap = H.snap()
    w = _weights_from(request.form)
    res = opt.replenishment_plan(snap, None, budget=H.fnum(request.form.get("budget")), truck_kg=H.fnum(request.form.get("truck_kg")), warehouse_m3=H.fnum(request.form.get("warehouse_m3")),
                                 service_floor=(H.fnum(request.form.get("service_floor"), 90) or 90) / 100.0, weights=w, enforce_service=bool(request.form.get("enforce")))
    if res["status"] != "OPTIMAL":
        H.err("No feasible plan: " + " ".join(res.get("explanation", [])))
        return redirect(url_for("optimization.home"))
    n = 0
    for p in res["plan"]:
        if p["qty"] > 0:
            try:
                action_service.create_action("CREATE_PO", item_id=p["item_id"], location_id=p["location_id"], qty=p["qty"], supplier_id=p["supplier_id"], why=f"Optimised replenishment plan (MILP): {p['sku']} @ {p['location']}",
                                             payload={"arrival_days": p["lead_time"], "optimizer": True}, created_by=H.actor())
                n += 1
            except action_service.ActionError:
                continue
    db.session.commit()
    H.ok(f"{n} purchase actions created from the optimised plan (each still simulated, policy-checked and routed for approval).")
    return redirect(url_for("actions.center"))


@bp.route("/rebalancing")
def rebalancing():
    if not H.has_data():
        return render_template("optimization/rebalancing.html", empty=True)
    snap, f = H.snap(), H.flt()
    require_full = request.args.get("full") == "1"
    plan = opt.rebalancing_plan(snap, f, require_full=require_full)
    return render_template("optimization/rebalancing.html", empty=False, plan=plan, require_full=require_full)


@bp.route("/rebalancing/act", methods=["POST"])
@require("create_action")
def rebalancing_act():
    snap = H.snap()
    plan = opt.rebalancing_plan(snap, H.flt())
    idx = int(request.form.get("idx", -1))
    if not (0 <= idx < len(plan["transfers"])):
        H.err("Transfer not found (data may have changed). Reload the page.")
        return redirect(url_for("optimization.rebalancing"))
    t = plan["transfers"][idx]
    try:
        a = action_service.create_action("TRANSFER_STOCK", item_id=t["item_id"], location_id=t["to_id"], to_location_id=t["to_id"], qty=t["qty"], cost=t["freight_cost"],
                                         why=f"Rebalance {t['qty']:,.0f} × {t['sku']} from {t['from']} to {t['to']}", payload={"from_location_id": t["from_id"], "arrival_days": t["eta_days"], "distance_km": t["distance_km"]},
                                         created_by=H.actor())
        db.session.commit()
        H.ok(f"Action {a.action_no}: {a.status.replace('_', ' ').title()}")
        return redirect(url_for("actions.action_detail", action_id=a.id))
    except action_service.ActionError as e:
        db.session.rollback()
        H.err(str(e))
        return redirect(url_for("optimization.rebalancing"))
