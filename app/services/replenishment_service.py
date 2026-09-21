"""Replenishment Control Center service (section 36) — turns policies +
current inventory position into concrete, explainable order recommendations."""
import json
from app.extensions import db
from app.models import ReplenishmentPolicy, SafetyStockPolicy, Item, Location, Recommendation
from app.services import inventory_service, demand_stats
from app.optimization import replenishment as repl_opt
from app.optimization.eoq import eoq as calc_eoq
from app.optimization.reorder_point import reorder_point as calc_rop


def generate_replenishment_recommendations(persist=True):
    recs = []
    policies = ReplenishmentPolicy.query.all()
    for policy in policies:
        item = policy.item
        location = policy.location
        pos = inventory_service.inventory_position(item.id, location.id)
        position_qty = pos["inventory_position"]
        d = demand_stats.daily_demand_stats(item.id, location.id)
        ss_policy = SafetyStockPolicy.query.filter_by(item_id=item.id, location_id=location.id).first()
        ss = ss_policy.effective_safety_stock if ss_policy else 0.0

        if policy.policy_type == "min_max" and policy.min_qty is not None and policy.max_qty is not None:
            rec = repl_opt.min_max_recommendation(position_qty, policy.min_qty, policy.max_qty,
                                                    item.moq, item.order_multiple)
        elif policy.policy_type == "order_up_to":
            lt = item.primary_supplier.lead_time_mean_days if item.primary_supplier else 14
            target = repl_opt.order_up_to_level(d["avg_demand_daily"], location.review_period_days, lt, ss)
            rec = repl_opt.order_up_to_recommendation(position_qty, target["order_up_to_level"],
                                                        item.moq, item.order_multiple)
            rec["target_detail"] = target
        else:  # rop_eoq default
            lt = item.primary_supplier.lead_time_mean_days if item.primary_supplier else 14
            rop = policy.reorder_point or calc_rop(d["avg_demand_daily"], lt, ss)["reorder_point"]
            annual_demand = d["avg_demand_daily"] * 365
            eoq_qty = policy.order_quantity or calc_eoq(annual_demand, 750.0, item.unit_cost * 0.22)["eoq"]
            rec = repl_opt.rop_eoq_recommendation(position_qty, rop, eoq_qty, item.moq, item.order_multiple)

        rec.update({"item": item.sku, "location": location.code, "item_id": item.id,
                    "location_id": location.id, "inventory_position": position_qty})
        recs.append(rec)

        if persist and rec.get("trigger") and rec.get("recommended_quantity", 0) > 0:
            existing = Recommendation.query.filter_by(
                rec_type="replenishment", item_id=item.id, destination_location_id=location.id,
                status="PENDING",
            ).first()
            if not existing:
                supplier_lt = item.primary_supplier.lead_time_mean_days if item.primary_supplier else 14
                reason = {
                    "policy": rec["policy"],
                    "inventory_position": position_qty,
                    "trigger_level": rec.get("reason") or f"Below trigger for {rec['policy']}",
                    "avg_demand_daily": d["avg_demand_daily"],
                    "lead_time_days": supplier_lt,
                    "constraint_detail": rec.get("constraint_detail"),
                }
                db.session.add(Recommendation(
                    rec_type="replenishment", item_id=item.id, destination_location_id=location.id,
                    quantity=rec["recommended_quantity"], reason_json=json.dumps(reason),
                    expected_impact_json=json.dumps({
                        "prevents_stockout": True,
                        "supplier": item.primary_supplier.name if item.primary_supplier else None,
                    }),
                    confidence="HIGH" if d["n_periods"] >= 8 else "MEDIUM",
                    cost_estimate=round(rec["recommended_quantity"] * item.unit_cost, 2),
                    autonomy_level=1, status="PENDING",
                ))
    if persist:
        db.session.commit()
    return recs
