import json
from flask import Blueprint, render_template, request, flash, redirect, url_for

from app.models import Location, OptimizationRun
from app.services import optimization_service

bp = Blueprint("optimization", __name__, url_prefix="/optimization")


@bp.route("/")
def home():
    runs = OptimizationRun.query.order_by(OptimizationRun.created_at.desc()).limit(15).all()
    for r in runs:
        r.result_summary = json.loads(r.result_summary_json) if r.result_summary_json else {}
    dcs = Location.query.filter_by(node_type="dc").all()
    return render_template("optimization/home.html", runs=runs, dcs=dcs)


@bp.route("/safety-stock/run", methods=["POST"])
def run_safety_stock():
    result = optimization_service.recompute_safety_stock()
    flash(f"Safety stock recomputed for {result['updated']} SKU-locations "
          f"({len(result['infeasible'])} skipped as infeasible - see run detail).", "success")
    return redirect(url_for("optimization.home"))


@bp.route("/meio", methods=["GET", "POST"])
def meio():
    result = None
    if request.method == "POST":
        central = request.form.get("central", "CENTRAL-DC")
        regionals = request.form.getlist("regionals") or ["DC-NORTH", "DC-SOUTH", "DC-WEST"]
        result = optimization_service.run_meio_comparison(central, regionals)
        if result.get("status") == "INFEASIBLE":
            flash(f"MEIO optimization infeasible: {result['reason']}", "warning")
            result = None
    dcs = Location.query.filter_by(node_type="dc").all()
    central_candidates = [d for d in dcs if d.parent_location_id is None]
    return render_template("optimization/meio.html", result=result, dcs=dcs, central_candidates=central_candidates)


@bp.route("/rebalancing", methods=["GET", "POST"])
def rebalancing():
    result = []
    if request.method == "POST":
        target_dos = int(request.form.get("target_dos_days", 14))
        result = optimization_service.run_rebalancing(target_dos)
        flash(f"{len(result)} transfer recommendation(s) generated.", "success")
    return render_template("optimization/rebalancing.html", result=result)
