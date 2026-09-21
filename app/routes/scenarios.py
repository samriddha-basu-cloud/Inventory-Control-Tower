import json
from flask import Blueprint, render_template, request, redirect, url_for, flash

from app.models import Scenario
from app.services import scenario_service

bp = Blueprint("scenarios", __name__, url_prefix="/scenarios")


@bp.route("/")
def list_scenarios():
    scenarios = Scenario.query.order_by(Scenario.created_at.desc()).all()
    return render_template("scenarios/list.html", scenarios=scenarios)


@bp.route("/run", methods=["POST"])
def run():
    name = request.form.get("name") or "Untitled scenario"
    assumptions = {
        "demand_change_pct": float(request.form.get("demand_change_pct", 0) or 0),
        "lead_time_delta_days": float(request.form.get("lead_time_delta_days", 0) or 0),
        "safety_stock_change_pct": float(request.form.get("safety_stock_change_pct", 0) or 0),
    }
    sl = request.form.get("service_level_target_pct")
    if sl:
        assumptions["service_level_target_pct"] = float(sl)

    scenario, results = scenario_service.run_scenario(name, assumptions)
    flash(f"Scenario '{name}' completed: working capital change "
          f"₹{results['delta']['working_capital_change']:,.0f}, stockout-risk change "
          f"{results['delta']['stockout_risk_change']:+d} SKU-locations.", "success")
    return redirect(url_for("scenarios.detail", scenario_id=scenario.id))


@bp.route("/<int:scenario_id>")
def detail(scenario_id):
    scenario = Scenario.query.get_or_404(scenario_id)
    results = json.loads(scenario.results_json) if scenario.results_json else {}
    assumptions = json.loads(scenario.assumptions_json) if scenario.assumptions_json else {}
    return render_template("scenarios/detail.html", scenario=scenario, results=results, assumptions=assumptions)


@bp.route("/compare")
def compare():
    ids = [int(i) for i in request.args.get("ids", "").split(",") if i.strip().isdigit()]
    comparison = scenario_service.compare_scenarios(ids) if ids else []
    all_scenarios = Scenario.query.order_by(Scenario.created_at.desc()).all()
    return render_template("scenarios/compare.html", comparison=comparison, all_scenarios=all_scenarios, ids=ids)
