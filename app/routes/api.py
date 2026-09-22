"""REST-ready JSON API. Machine clients use X-API-Key; browser sessions use the same RBAC as the UI."""
from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, jsonify, request

from ..extensions import db
from ..models import Action, Alert, Approval, AuditLog, Forecast, Incident, Location, Scenario, Supplier, Item
from ..services import (action_service, alert_service, event_service, forecast_service, ingestion_service, optimization_service as opt, risk_service, search_service, settings_service as S)
from ..utils.jsonutil import _clean
from ..utils.security import require
from . import helpers as H

bp = Blueprint("api", __name__)


def _pair_json(inp, r):
    return {"sku": inp.sku, "location": inp.loc_code, "on_hand": r.pos["on_hand"], "available": r.pos["available"], "allocated": r.pos["allocated"], "in_transit": r.pos["in_transit"], "on_order": r.pos["on_order"],
            "backorder": r.pos["backorder"], "position": r.pos["position"], "net_available": r.pos["net_available"], "safety_stock": r.ss, "safety_stock_method": r.ss_method, "reorder_point": r.rop, "eoq": r.eoq,
            "demand_per_day": r.d_mean, "days_supply": r.days_supply, "lead_time_days": r.lt_plan, "lead_time_static": r.lt_static, "lead_time_p90": r.lt_p90, "stockout_probability": r.stockout_prob,
            "risk": r.risk_level, "value": r.on_hand_value, "inventory_class": r.inv_class, "confidence": r.confidence, "recommendation": {"qty": r.rec["qty"], "policy": r.rec["policy"], "order_date": r.rec["order_date"],
            "arrival_date": r.rec["arrival_date"]}}


@bp.route("/search")
def search():
    return jsonify({"results": search_service.search(request.args.get("q", ""))})


@bp.route("/inventory")
@require("view")
def inventory():
    snap, f = H.snap(), H.flt()
    for k in ("sku", "location", "supplier", "region", "family", "industry"):
        if request.args.get(k):
            setattr(f, k, request.args[k])
    rows = [_pair_json(i, r) for i, r in snap.rows(f)]
    lim = min(int(request.args.get("limit", 500)), 5000)
    return jsonify({"count": len(rows), "as_of": snap.today, "items": rows[:lim]})


@bp.route("/inventory/<sku>")
@require("view")
def inventory_sku(sku):
    snap = H.snap()
    keys = [k for k in snap.inputs if snap.inputs[k].sku == sku]
    if not keys:
        return jsonify({"error": f"Unknown SKU '{sku}'"}), 404
    return jsonify({"sku": sku, "segment": {k: v for k, v in snap.item_seg[keys[0][0]].items()}, "locations": [_pair_json(snap.inputs[k], snap.results[k]) for k in keys]})


@bp.route("/location/<code>")
@require("view")
def location(code):
    snap = H.snap()
    l = Location.query.filter_by(code=code).first()
    if not l:
        return jsonify({"error": "Unknown location"}), 404
    return jsonify({"location": code, "name": l.name, "type": l.loc_type, "capacity_units": l.capacity_units, "items": [_pair_json(snap.inputs[k], snap.results[k]) for k in snap.keys_for_loc(l.id)]})


@bp.route("/supplier/<code>")
@require("view")
def supplier(code):
    snap = H.snap()
    s = Supplier.query.filter_by(code=code).first()
    if not s:
        return jsonify({"error": "Unknown supplier"}), 404
    return jsonify(risk_service.supplier_scorecards(snap).get(s.id))


@bp.route("/demand")
@require("view")
def demand():
    snap = H.snap()
    out = []
    for k, i in snap.inputs.items():
        if request.args.get("sku") and i.sku != request.args["sku"]:
            continue
        if request.args.get("location") and i.loc_code != request.args["location"]:
            continue
        wks = i.extra.get("hist_weeks", [])
        out.append({"sku": i.sku, "location": i.loc_code, "weekly_actuals": [{"period_start": w, "qty": q} for w, q in zip(wks, i.hist_weekly)][-int(request.args.get("weeks", 26)):]})
    return jsonify({"count": len(out), "items": out[:200]})


@bp.route("/forecast", methods=["GET", "POST"])
def forecast():
    if request.method == "POST":
        from ..utils.security import has_perm
        if not has_perm("ingest"):
            return jsonify({"error": "permission 'ingest' required"}), 403
        try:
            res = forecast_service.ingest_fit_payload(request.get_json(force=True, silent=False) or {}, actor=H.actor())
            db.session.commit()
            return jsonify(res), 200 if not res["rejected"] else 207
        except (forecast_service.ContractError, Exception) as e:
            db.session.rollback()
            if isinstance(e, forecast_service.ContractError):
                return jsonify({"error": str(e)}), 400
            return jsonify({"error": "Invalid JSON body"}), 400
    if not H.has_data():
        return jsonify({"count": 0, "items": []})
    snap = H.snap()
    rows = [{"sku": i.sku, "location": i.loc_code, "type": i.forecast_type, "weekly": i.forecast_weekly[:int(request.args.get("weeks", 13))]} for i in snap.inputs.values()
            if (not request.args.get("sku") or i.sku == request.args["sku"]) and i.forecast_weekly]
    return jsonify({"count": len(rows), "items": rows[:300]})


@bp.route("/forecast/contract")
def forecast_contract():
    return jsonify(forecast_service.FIT_CONTRACT)


@bp.route("/forecast/signals")
@require("view")
def forecast_signals():
    return jsonify({"signals": forecast_service.demand_signals(request.args.get("sku"), request.args.get("location"), int(request.args.get("weeks", 26)))[:300]})


@bp.route("/replenishment")
@require("view")
def replenishment():
    snap = H.snap()
    rows = [{"sku": i.sku, "location": i.loc_code, **_pair_json(i, r)["recommendation"], "position": r.pos["position"], "rop": r.rop, "raw_qty": r.rec["raw_qty"], "triggered": r.rec["triggered"],
             "post_order_position": r.rec["post_order_position"], "risk_if_not_ordered": r.rec["risk_if_not_ordered"]["probability"], "notes": r.rec["notes"]} for i, r in snap.rows(H.flt()) if r.rec["qty"] > 0 or request.args.get("all")]
    return jsonify({"count": len(rows), "items": rows[:1000]})


@bp.route("/safety-stock")
@require("view")
def safety_stock():
    snap = H.snap()
    return jsonify({"items": [{"sku": i.sku, "location": i.loc_code, "method": r.ss_method, "service_level": r.service_level, "z": r.z, "safety_stock": r.ss, "reorder_point": r.rop, "formula": r.ss_formula,
                               "inputs": r.ss_inputs} for i, r in snap.rows(H.flt())][:1000]})


@bp.route("/alerts")
@require("view")
def alerts():
    q = Alert.query.filter(Alert.status.in_(alert_service.OPEN_STATUSES), Alert.suppressed.is_(False))
    if request.args.get("severity"):
        q = q.filter_by(severity=request.args["severity"].upper())
    rows = q.order_by(Alert.priority_score.desc()).limit(int(request.args.get("limit", 200))).all()
    return jsonify({"count": len(rows), "items": [{"alert_no": a.alert_no, "type": a.alert_type, "severity": a.severity, "status": a.status, "priority_score": a.priority_score, "title": a.title, "owner": a.owner,
                                                   "impact": a.impact, "incident": a.incident.incident_no if a.incident else None, "explain": alert_service.explain(a)} for a in rows]})


@bp.route("/incidents")
@require("view")
def incidents():
    rows = Incident.query.order_by(Incident.priority_score.desc()).all()
    return jsonify({"count": len(rows), "items": [{"incident_no": i.incident_no, "title": i.title, "severity": i.severity, "status": i.status, "root_cause": i.root_cause, "impact": i.impact, "priority_score": i.priority_score,
                                                   "explain": i.explain} for i in rows]})


@bp.route("/scenarios")
@require("view")
def scenarios():
    return jsonify({"items": [{"scenario_no": s.scenario_no, "name": s.name, "created_by": s.created_by, "created_at": s.created_at, "base_dataset": s.base_dataset, "changes": s.changes,
                               "deltas": (s.results or {}).get("deltas")} for s in Scenario.query.order_by(Scenario.id.desc()).limit(50).all()]})


@bp.route("/rebalancing")
@require("view")
def rebalancing():
    plan = opt.rebalancing_plan(H.snap(), H.flt())
    return jsonify({"status": plan["status"], "transfers": [{k: v for k, v in t.items() if k not in ("before", "after")} | {"before": t["before"], "after": t["after"]} for t in plan["transfers"]], "unmet": plan["unmet"],
                    "explanation": plan["explanation"]})


@bp.route("/optimization")
@require("view")
def optimization():
    res = opt.replenishment_plan(H.snap(), H.flt())
    return jsonify({"status": res["status"], "explanation": res["explanation"], "objective": res.get("objective"), "constraints": res.get("constraints"), "plan": res.get("plan")})


@bp.route("/actions", methods=["GET", "POST"])
def actions():
    if request.method == "POST":
        from ..utils.security import has_perm
        if not has_perm("create_action"):
            return jsonify({"error": "permission 'create_action' required"}), 403
        d = request.get_json(silent=True) or {}
        try:
            item = Item.query.filter_by(sku=d.get("sku", "")).first()
            loc = Location.query.filter_by(code=d.get("location", "")).first()
            if not item or not loc:
                return jsonify({"error": "Missing SKU or location"}), 400
            a = action_service.create_action(d.get("type", ""), item_id=item.id, location_id=loc.id, qty=float(d.get("qty", 0)), why=str(d.get("why", ""))[:500], payload=d.get("payload") or {}, created_by=H.actor())
            db.session.commit()
            return jsonify({"action_no": a.action_no, "status": a.status, "approval_required": a.approval_required, "autonomy": a.autonomy, "policy_check": a.policy_check}), 201
        except (action_service.ActionError, ValueError) as e:
            db.session.rollback()
            return jsonify({"error": str(e)}), 400
    rows = Action.query.order_by(Action.id.desc()).limit(200).all()
    return jsonify({"items": [{"action_no": a.action_no, "type": a.action_type, "sku": a.item.sku if a.item else None, "location": a.location.code if a.location else None, "qty": a.qty, "cost": a.cost,
                               "status": a.status, "mode": a.mode, "risk": a.risk_level, "confidence": a.confidence, "approval_required": a.approval_required, "required_role": a.required_role,
                               "why": a.why} for a in rows]})


@bp.route("/approvals")
@require("view")
def approvals():
    return jsonify({"items": [{"action": Action.query.get(a.action_id).action_no, "approver": a.approver, "role": a.role, "decision": a.decision, "comment": a.comment, "at": a.decided_at}
                              for a in Approval.query.order_by(Approval.id.desc()).limit(200).all()]})


@bp.route("/audit")
@require("view")
def audit():
    q = AuditLog.query
    if request.args.get("category"):
        q = q.filter_by(category=request.args["category"])
    rows = q.order_by(AuditLog.id.desc()).limit(min(int(request.args.get("limit", 200)), 2000)).all()
    return jsonify({"items": [{"at": a.timestamp, "actor": a.actor, "category": a.category, "entity": f"{a.entity_type}:{a.entity_id}", "event": a.event, "details": a.details, "trace": a.trace} for a in rows]})


@bp.route("/events", methods=["POST"])
@require("ingest")
def events():
    d = request.get_json(silent=True) or {}
    ev = event_service.publish(d.get("event_type", ""), d.get("payload") or {}, source=d.get("source", "API"), event_id=d.get("event_id"))
    db.session.commit()
    return jsonify({"event_id": ev.event_id, "status": ev.status, "error": ev.error}), 200 if ev.status in ("PROCESSED", "DUPLICATE") else 422


@bp.route("/ingest/<entity>", methods=["POST"])
@require("ingest")
def ingest(entity):
    d = request.get_json(silent=True) or {}
    recs = d.get("records")
    if not isinstance(recs, list) or not recs:
        return jsonify({"error": "'records' must be a non-empty list"}), 400
    try:
        rep = ingestion_service.ingest_records(entity, recs, commit=bool(d.get("commit")), all_or_nothing=bool(d.get("all_or_nothing")), source_label="API", actor=H.actor())
        db.session.commit() if rep.committed else db.session.rollback()
    except ingestion_service.IngestError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    return jsonify({"entity": entity, "total": rep.total, "valid": rep.valid, "created": rep.created, "updated": rep.updated, "skipped": rep.skipped, "committed": rep.committed, "errors": rep.errors[:100],
                    "warnings": rep.warnings[:20]}), 200 if not rep.errors else 207


@bp.route("/health")
def health():
    return jsonify({"status": "ok", "as_of": S.today()})


@bp.errorhandler(404)
def _nf(e):
    return jsonify({"error": "not found"}), 404
