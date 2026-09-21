"""
REST API (section 116). JSON in/out. This is a functional first-party API
surface for the endpoints the spec calls out; it is not yet OpenAPI-annotated
(see docs/api.md for the documented contract).
"""
from flask import Blueprint, jsonify, request
from werkzeug.exceptions import BadRequest

from app.extensions import db
from app.models import Item, Location, Alert, Recommendation, Approval
from app.services import inventory_service, risk_service, demand_stats, alert_service
from app.services import optimization_service, scenario_service, allocation_service, replenishment_service
from app.analytics.aging import age_days, aging_summary

bp = Blueprint("api", __name__)


def _require_ids():
    item_id = request.args.get("item_id", type=int)
    location_id = request.args.get("location_id", type=int)
    if not item_id or not location_id:
        raise BadRequest("item_id and location_id query parameters are required")
    return item_id, location_id


@bp.errorhandler(BadRequest)
def handle_bad_request(e):
    return jsonify({"error": str(e)}), 400


@bp.route("/inventory")
def api_inventory():
    rows = []
    for item in Item.query.filter_by(is_active=True).limit(500).all():
        for loc in Location.query.all():
            qtys = inventory_service.status_quantities(item.id, loc.id)
            if any(qtys.values()):
                rows.append({"sku": item.sku, "location": loc.code, **qtys})
    return jsonify(rows)


@bp.route("/inventory/position")
def api_inventory_position():
    item_id, location_id = _require_ids()
    return jsonify(inventory_service.inventory_position(item_id, location_id))


@bp.route("/inventory/availability")
def api_inventory_availability():
    item_id, location_id = _require_ids()
    return jsonify({"available": inventory_service.available_quantity(item_id, location_id),
                     "atp": inventory_service.atp(item_id, location_id)})


@bp.route("/inventory/risk")
def api_inventory_risk():
    item_id, location_id = _require_ids()
    return jsonify({"stockout": risk_service.stockout_risk(item_id, location_id),
                     "excess": risk_service.excess_detection(item_id, location_id)})


@bp.route("/inventory/aging")
def api_inventory_aging():
    from app.models import InventoryLedger
    rows = []
    q = InventoryLedger.query.filter_by(status="ON_HAND")
    item_id = request.args.get("item_id", type=int)
    if item_id:
        q = q.filter_by(item_id=item_id)
    for ledger in q.all():
        ref = ledger.manufacture_date or ledger.as_of.date()
        rows.append({"quantity": ledger.quantity, "age_days": age_days(ref),
                     "unit_cost": ledger.item.unit_cost if ledger.item else 0})
    return jsonify(aging_summary(rows))


@bp.route("/optimization/run", methods=["POST"])
def api_optimization_run():
    payload = request.get_json(silent=True) or {}
    run_type = payload.get("run_type", "safety_stock")
    if run_type == "safety_stock":
        return jsonify(optimization_service.recompute_safety_stock())
    if run_type == "meio":
        return jsonify(optimization_service.run_meio_comparison(
            payload.get("central", "CENTRAL-DC"), payload.get("regionals", ["DC-NORTH", "DC-SOUTH", "DC-WEST"])))
    if run_type == "rebalancing":
        return jsonify(optimization_service.run_rebalancing(payload.get("target_dos_days", 14)))
    raise BadRequest(f"Unknown run_type: {run_type}")


@bp.route("/scenario/run", methods=["POST"])
def api_scenario_run():
    payload = request.get_json(silent=True) or {}
    name = payload.get("name", "API scenario")
    assumptions = payload.get("assumptions", {})
    scenario, results = scenario_service.run_scenario(name, assumptions)
    return jsonify({"scenario_id": scenario.id, "results": results})


@bp.route("/allocation/recommend", methods=["POST"])
def api_allocation_recommend():
    payload = request.get_json(silent=True) or {}
    item_id = payload.get("item_id")
    location_id = payload.get("location_id")
    rule = payload.get("rule", "priority_customer")
    if not item_id or not location_id:
        raise BadRequest("item_id and location_id are required")
    return jsonify(allocation_service.allocate_item(item_id, location_id, rule))


@bp.route("/replenishment/recommend", methods=["POST"])
def api_replenishment_recommend():
    persist = bool((request.get_json(silent=True) or {}).get("persist", False))
    recs = replenishment_service.generate_replenishment_recommendations(persist=persist)
    return jsonify([r for r in recs if r.get("trigger")])


@bp.route("/alerts")
def api_alerts():
    status = request.args.get("status")
    q = Alert.query
    if status:
        q = q.filter_by(status=status)
    alerts = q.order_by(Alert.severity).limit(500).all()
    return jsonify([{"id": a.id, "type": a.alert_type, "sku": a.item.sku if a.item else None,
                      "location": a.location.code if a.location else None, "severity": a.severity,
                      "financial_impact": a.financial_impact, "status": a.status, "message": a.message}
                     for a in alerts])


@bp.route("/actions/approve", methods=["POST"])
def api_approve_action():
    payload = request.get_json(silent=True) or {}
    rec_id = payload.get("recommendation_id")
    decision = payload.get("decision", "APPROVE")
    if not rec_id:
        raise BadRequest("recommendation_id is required")
    rec = Recommendation.query.get(rec_id)
    if not rec:
        return jsonify({"error": "Recommendation not found"}), 404
    db.session.add(Approval(recommendation_id=rec.id, decision=decision,
                             reason=payload.get("reason", ""), decided_by=payload.get("decided_by", "api")))
    rec.status = {"APPROVE": "APPROVED", "REJECT": "REJECTED", "MODIFY": "MODIFIED"}.get(decision, "PENDING")
    db.session.commit()
    return jsonify({"recommendation_id": rec.id, "status": rec.status})
