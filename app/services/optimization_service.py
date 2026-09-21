"""Optimization service — recomputes safety stock policies network-wide,
runs single-echelon vs multi-echelon comparisons, and generates rebalancing
recommendations. Every run is persisted to OptimizationRun for
reproducibility/audit (sections 74, 163)."""
import json
import time
from app.extensions import db
from app.models import (SafetyStockPolicy, Item, Location, OptimizationRun, Recommendation,
                         TransferOrder)
from app.services import demand_stats, inventory_service
from app.optimization.safety_stock import combined_variability
from app.optimization.single_echelon import optimize_single_echelon
from app.optimization.multi_echelon import optimize_two_echelon, compare_single_vs_multi_echelon
from app.optimization import network as network_opt


def recompute_safety_stock(persist=True):
    start = time.time()
    updated = 0
    infeasible = []
    for policy in SafetyStockPolicy.query.all():
        item = policy.item
        if not item or not item.primary_supplier:
            infeasible.append({"item": item.sku if item else None, "reason": "No primary supplier / lead time"})
            continue
        d = demand_stats.daily_demand_stats(item.id, policy.location_id)
        if d["n_periods"] < 2:
            infeasible.append({"item": item.sku, "reason": "Insufficient demand history (<2 periods)"})
            continue
        calc = combined_variability(
            d["avg_demand_daily"], d["std_dev_demand_daily"],
            item.primary_supplier.lead_time_mean_days, item.primary_supplier.lead_time_std_days,
            policy.service_level_pct,
        )
        policy.calculated_safety_stock = calc["safety_stock"]
        updated += 1

    run = OptimizationRun(
        run_type="safety_stock", objective="Recompute statistical safety stock from current demand history",
        status="COMPLETED", result_summary_json=json.dumps({"updated": updated, "infeasible": infeasible}),
        runtime_ms=int((time.time() - start) * 1000),
    )
    if persist:
        db.session.add(run)
        db.session.commit()
    return {"updated": updated, "infeasible": infeasible, "run_id": run.id if persist else None}


def run_meio_comparison(central_location_code, regional_location_codes, item_ids=None):
    """Build single-echelon vs multi-echelon comparison for a central node
    feeding a list of regional nodes, for the given items (or all items with
    demand at those regionals)."""
    central = Location.query.filter_by(code=central_location_code).first()
    regionals = Location.query.filter(Location.code.in_(regional_location_codes)).all()
    if not central or not regionals:
        return {"status": "INFEASIBLE", "reason": "Central or regional locations not found."}

    items = Item.query.filter(Item.id.in_(item_ids)).all() if item_ids else Item.query.filter_by(is_active=True).all()

    per_item_results = []
    for item in items:
        if not item.primary_supplier:
            continue
        downstream_nodes = []
        single_echelon_nodes = []
        for loc in regionals:
            d = demand_stats.daily_demand_stats(item.id, loc.id)
            if d["avg_demand_daily"] <= 0:
                continue
            downstream_nodes.append({
                "node": loc.code, "avg_demand_daily": d["avg_demand_daily"],
                "std_dev_demand_daily": d["std_dev_demand_daily"],
                "internal_lead_time_days": 2.0, "internal_std_dev_lead_time_days": 0.5,
                "service_level_pct": item.service_level_target_pct, "unit_cost": item.unit_cost,
            })
            # Single-echelon baseline: each regional node sources directly from the
            # external supplier (no network coordination), so it must cover the
            # FULL external lead time on its own - the fair comparison for section 39.
            single_echelon_nodes.append({
                "node": loc.code, "avg_demand_daily": d["avg_demand_daily"],
                "std_dev_demand_daily": d["std_dev_demand_daily"],
                "lead_time_days": item.primary_supplier.lead_time_mean_days,
                "std_dev_lead_time_days": item.primary_supplier.lead_time_std_days,
                "service_level_pct": item.service_level_target_pct, "unit_cost": item.unit_cost,
            })
        if not downstream_nodes:
            continue

        single = optimize_single_echelon(single_echelon_nodes)
        upstream = {
            "node": central.code,
            "external_lead_time_days": item.primary_supplier.lead_time_mean_days,
            "external_std_dev_lead_time_days": item.primary_supplier.lead_time_std_days,
            "service_level_pct": item.service_level_target_pct, "unit_cost": item.unit_cost,
        }
        multi = optimize_two_echelon(upstream, downstream_nodes)
        comparison = compare_single_vs_multi_echelon(single, multi)
        per_item_results.append({"item": item.sku, "single": single, "multi": multi, "comparison": comparison})

    if not per_item_results:
        return {"status": "INFEASIBLE", "reason": "No items with demand history at the selected regional nodes."}

    total_release = sum(r["comparison"]["working_capital_release"] for r in per_item_results)
    run = OptimizationRun(
        run_type="meio", objective="Minimize network safety stock via risk pooling at central node",
        status="COMPLETED",
        input_snapshot_json=json.dumps({"central": central_location_code, "regionals": regional_location_codes}),
        result_summary_json=json.dumps({"items_evaluated": len(per_item_results),
                                         "total_working_capital_release": round(total_release, 2)}),
    )
    db.session.add(run)
    db.session.commit()

    return {"status": "COMPLETED", "run_id": run.id, "sku_results": per_item_results,
            "total_working_capital_release": round(total_release, 2)}


def _haversine_km(lat1, lon1, lat2, lon2):
    import math
    if None in (lat1, lon1, lat2, lon2):
        return None
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return r * 2 * math.atan2(a ** 0.5, (1 - a) ** 0.5)


def run_rebalancing(target_dos_days=14, persist_recommendations=True, cost_per_km_per_unit=0.02,
                     km_per_transit_day=600):
    """cost_per_km_per_unit / km_per_transit_day are documented, configurable road-freight
    assumptions used only when no real transportation-lane rate is configured (section 8/48)."""
    items = Item.query.filter_by(is_active=True).all()
    locations = Location.query.filter(Location.node_type.in_(["dc", "store"])).all()
    all_recs = []

    cost_matrix, transit_matrix = {}, {}
    for a in locations:
        for b in locations:
            if a.id == b.id:
                continue
            dist = _haversine_km(a.latitude, a.longitude, b.latitude, b.longitude)
            if dist is None:
                continue
            cost_matrix[(a.code, b.code)] = round(dist * cost_per_km_per_unit, 3)
            transit_matrix[(a.code, b.code)] = max(1, round(dist / km_per_transit_day, 1))

    for item in items:
        node_positions = []
        for loc in locations:
            d = demand_stats.daily_demand_stats(item.id, loc.id)
            avail = inventory_service.available_quantity(item.id, loc.id)
            if avail == 0 and d["avg_demand_daily"] == 0:
                continue
            node_positions.append({"location": loc.code, "available": avail,
                                    "avg_demand_daily": d["avg_demand_daily"]})
        if len(node_positions) < 2:
            continue
        classified = network_opt.identify_surplus_deficit(node_positions, target_dos_days)
        transfers = network_opt.recommend_transfers(classified, target_dos_days,
                                                      cost_per_unit_matrix=cost_matrix,
                                                      transit_days_matrix=transit_matrix)
        for t in transfers:
            t["item"] = item.sku
            t["item_id"] = item.id
            all_recs.append(t)

            if persist_recommendations:
                src = Location.query.filter_by(code=t["source"]).first()
                dst = Location.query.filter_by(code=t["destination"]).first()
                reason = {
                    "source_days_of_supply": next(
                        (n["days_of_supply"] for n in classified if n["location"] == t["source"]), None),
                    "destination_days_of_supply": next(
                        (n["days_of_supply"] for n in classified if n["location"] == t["destination"]), None),
                    "target_dos_days": target_dos_days,
                    "explanation": t["reason"],
                }
                db.session.add(Recommendation(
                    rec_type="transfer", item_id=item.id, source_location_id=src.id if src else None,
                    destination_location_id=dst.id if dst else None, quantity=t["quantity"],
                    reason_json=json.dumps(reason),
                    expected_impact_json=json.dumps({"transit_days": t["transit_days"],
                                                      "transport_cost": t["transport_cost"]}),
                    confidence="HIGH", cost_estimate=t["transport_cost"], autonomy_level=1, status="PENDING",
                ))
    if persist_recommendations:
        db.session.commit()
    return all_recs
