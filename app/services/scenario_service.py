"""
Digital Twin / What-If scenario engine (section 72).

Scenarios NEVER mutate live ledger/policy data. All assumptions are applied
to an in-memory snapshot pulled from the database, recomputed, and the
result (not the live state) is stored on the Scenario row.
"""
import json
import time
from datetime import date

from app.extensions import db
from app.models import Scenario, Item, Location, SafetyStockPolicy
from app.services import demand_stats, inventory_service, risk_service
from app.optimization.safety_stock import combined_variability
from app.optimization import network as network_opt


def run_scenario(name, assumptions, created_by="planner@demo.org"):
    """
    assumptions: {
        "demand_change_pct": float (e.g. 10 for +10%),
        "lead_time_delta_days": float,
        "safety_stock_change_pct": float,
        "service_level_target_pct": float | None,
    }
    """
    start = time.time()
    demand_change = assumptions.get("demand_change_pct", 0) / 100.0
    lt_delta = assumptions.get("lead_time_delta_days", 0)
    ss_change = assumptions.get("safety_stock_change_pct", 0) / 100.0
    sl_override = assumptions.get("service_level_target_pct")

    baseline_ss_value = 0.0
    scenario_ss_value = 0.0
    baseline_stockout_count = 0
    scenario_stockout_count = 0
    items_evaluated = 0
    detail_rows = []

    combos = db.session.query(SafetyStockPolicy.item_id, SafetyStockPolicy.location_id).all()
    for item_id, location_id in combos:
        item = Item.query.get(item_id)
        location = Location.query.get(location_id)
        policy = SafetyStockPolicy.query.filter_by(item_id=item_id, location_id=location_id).first()
        d = demand_stats.daily_demand_stats(item_id, location_id)
        if d["avg_demand_daily"] <= 0 or not item or not item.primary_supplier:
            continue
        items_evaluated += 1

        lt = item.primary_supplier.lead_time_mean_days
        lt_std = item.primary_supplier.lead_time_std_days
        sl = sl_override or policy.service_level_pct

        baseline_calc = combined_variability(d["avg_demand_daily"], d["std_dev_demand_daily"], lt, lt_std, sl)
        scenario_demand_mean = d["avg_demand_daily"] * (1 + demand_change)
        scenario_demand_std = d["std_dev_demand_daily"] * (1 + demand_change)
        scenario_lt = max(0, lt + lt_delta)
        scenario_calc = combined_variability(scenario_demand_mean, scenario_demand_std, scenario_lt, lt_std, sl)
        scenario_ss = scenario_calc["safety_stock"] * (1 + ss_change)

        baseline_ss_value += baseline_calc["safety_stock"] * item.unit_cost
        scenario_ss_value += scenario_ss * item.unit_cost

        avail = inventory_service.available_quantity(item_id, location_id)
        baseline_lt_demand = d["avg_demand_daily"] * lt
        scenario_lt_demand = scenario_demand_mean * scenario_lt
        if avail < baseline_lt_demand:
            baseline_stockout_count += 1
        if avail < scenario_lt_demand:
            scenario_stockout_count += 1

        detail_rows.append({
            "sku": item.sku, "location": location.code,
            "baseline_safety_stock": baseline_calc["safety_stock"],
            "scenario_safety_stock": round(scenario_ss, 2),
            "baseline_lead_time_demand": round(baseline_lt_demand, 1),
            "scenario_lead_time_demand": round(scenario_lt_demand, 1),
            "available": round(avail, 1),
        })

    working_capital_delta = scenario_ss_value - baseline_ss_value
    results = {
        "assumptions": assumptions,
        "items_evaluated": items_evaluated,
        "baseline": {
            "safety_stock_value": round(baseline_ss_value, 2),
            "sku_locations_at_stockout_risk": baseline_stockout_count,
        },
        "scenario": {
            "safety_stock_value": round(scenario_ss_value, 2),
            "sku_locations_at_stockout_risk": scenario_stockout_count,
        },
        "delta": {
            "working_capital_change": round(working_capital_delta, 2),
            "stockout_risk_change": scenario_stockout_count - baseline_stockout_count,
        },
        "detail": detail_rows[:200],  # cap payload size
        "runtime_ms": int((time.time() - start) * 1000),
    }

    scenario = Scenario(
        name=name, base_version="live", assumptions_json=json.dumps(assumptions),
        results_json=json.dumps(results), status="COMPLETED", created_by=created_by,
    )
    db.session.add(scenario)
    db.session.commit()
    return scenario, results


def compare_scenarios(scenario_ids):
    scenarios = Scenario.query.filter(Scenario.id.in_(scenario_ids)).all()
    comparison = []
    for s in scenarios:
        results = json.loads(s.results_json) if s.results_json else {}
        comparison.append({
            "id": s.id, "name": s.name, "created_at": s.created_at,
            "assumptions": json.loads(s.assumptions_json) if s.assumptions_json else {},
            "safety_stock_value": results.get("scenario", {}).get("safety_stock_value"),
            "stockout_risk_count": results.get("scenario", {}).get("sku_locations_at_stockout_risk"),
        })
    return comparison
