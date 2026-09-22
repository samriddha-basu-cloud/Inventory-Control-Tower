"""Alert center and root-cause incidents."""
from __future__ import annotations

from collections import Counter

from flask import Blueprint, abort, render_template, request

from ..extensions import db
from ..models import Alert, AuditLog, Incident, Recommendation
from ..services import alert_service
from ..utils.security import require
from . import helpers as H

bp = Blueprint("alerts", __name__)
SEV = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]


@bp.route("/alerts")
def list_alerts():
    view = request.args.get("view", "unresolved")
    sev, atype, show_sup = request.args.get("sev"), request.args.get("type"), request.args.get("suppressed") == "1"
    q = Alert.query
    if view in ("unresolved", "mine", "root", "financial", "service"):
        q = q.filter(Alert.status.in_(alert_service.OPEN_STATUSES))
    if not show_sup:
        q = q.filter(Alert.suppressed.is_(False))
    if view == "mine":
        q = q.filter(Alert.owner == H.role())
    if view == "root":
        q = q.filter(Alert.incident_id.isnot(None))
    if sev:
        q = q.filter(Alert.severity == sev)
    if atype:
        q = q.filter(Alert.alert_type == atype)
    alerts = q.order_by(Alert.priority_score.desc()).limit(600).all()
    if view == "financial":
        alerts.sort(key=lambda a: -max((a.impact or {}).get("value_at_risk", 0), (a.impact or {}).get("revenue_at_risk", 0)))
    if view == "service":
        alerts.sort(key=lambda a: -((a.impact or {}).get("service_impact", 0)))
    rows = [{"id": a.id, "no": a.alert_no, "sev": a.severity, "type": a.alert_type.replace("_", " ").title(), "title": a.title, "status": a.status, "pri": a.priority_score, "owner": a.owner, "when": a.first_seen,
             "value": max((a.impact or {}).get("value_at_risk", 0), (a.impact or {}).get("revenue_at_risk", 0)), "svc": (a.impact or {}).get("service_impact", 0), "tti": a.time_to_impact_days,
             "incident": a.incident.incident_no if a.incident else "", "inc_id": a.incident_id, "esc": "Escalated" if a.escalated else "", "occ": a.occurrences} for a in alerts]
    counts = Counter(a.severity for a in Alert.query.filter(Alert.status.in_(alert_service.OPEN_STATUSES), Alert.suppressed.is_(False)).all())
    types = sorted({a.alert_type for a in Alert.query.all()})
    return render_template("alerts/list.html", rows=rows, view=view, counts=counts, sev=sev, atype=atype, types=types, show_sup=show_sup, statuses=alert_service.STATUSES,
                           suppressed=Alert.query.filter_by(suppressed=True).count(), weights=alert_service.S.get("weights.priority"))


@bp.route("/alerts/<int:alert_id>")
def alert_detail(alert_id):
    a = Alert.query.get_or_404(alert_id)
    recs = Recommendation.query.filter_by(alert_id=a.id).all()
    audit = AuditLog.query.filter_by(entity_type="Alert", entity_id=str(a.id)).order_by(AuditLog.id.desc()).limit(20).all()
    return render_template("alerts/detail.html", a=a, ex=alert_service.explain(a), recs=recs, audit=audit, statuses=alert_service.STATUSES)


@bp.route("/alerts/<int:alert_id>/status", methods=["POST"])
@require("alert_manage")
def alert_status(alert_id):
    try:
        alert_service.set_status(alert_id, request.form.get("status", ""), H.actor(), request.form.get("note"))
        db.session.commit()
        H.ok("Alert status updated and recorded in the audit trail.")
    except ValueError as e:
        db.session.rollback()
        H.err(str(e))
    return H.back("alerts.list_alerts")


@bp.route("/root-cause")
def incidents():
    incs = Incident.query.filter(Incident.status.notin_(["Resolved", "Dismissed"])).order_by(Incident.priority_score.desc()).all()
    closed = Incident.query.filter(Incident.status.in_(["Resolved", "Dismissed"])).order_by(Incident.id.desc()).limit(20).all()
    rows = [{"id": i.id, "no": i.incident_no, "title": i.title, "sev": i.severity, "status": i.status, "cause": i.cause_type.replace("_", " ").title() if i.cause_type else "", "pri": i.priority_score,
             "alerts": i.impact.get("alerts"), "skus": len(i.impact.get("affected_skus", [])), "nodes": len(i.impact.get("affected_nodes", [])), "rev": i.impact.get("revenue_at_risk"),
             "prod": i.impact.get("production_at_risk"), "svc": i.impact.get("service_impact"), "inv": i.impact.get("inventory_impact"), "owner": i.owner, "root": i.root_cause} for i in incs]
    open_alerts = Alert.query.filter(Alert.status.in_(alert_service.OPEN_STATUSES), Alert.suppressed.is_(False)).count()
    clustered = Alert.query.filter(Alert.incident_id.isnot(None), Alert.status.in_(alert_service.OPEN_STATUSES)).count()
    return render_template("alerts/incidents.html", rows=rows, closed=closed, open_alerts=open_alerts, clustered=clustered)


@bp.route("/root-cause/<int:incident_id>")
def incident_detail(incident_id):
    i = Incident.query.get_or_404(incident_id)
    alerts = sorted(i.alerts, key=lambda a: -a.priority_score)
    recs = Recommendation.query.filter(Recommendation.alert_id.in_([a.id for a in alerts])).order_by(Recommendation.priority_score.desc()).all()
    return render_template("alerts/incident_detail.html", i=i, alerts=alerts, recs=recs, statuses=["New", "Acknowledged", "Investigating", "Action Proposed", "Approved", "Executed", "Resolved", "Dismissed"])


@bp.route("/root-cause/<int:incident_id>/status", methods=["POST"])
@require("alert_manage")
def incident_status(incident_id):
    i = Incident.query.get_or_404(incident_id)
    st = request.form.get("status", "")
    if st not in ["New", "Acknowledged", "Investigating", "Action Proposed", "Approved", "Executed", "Resolved", "Dismissed"]:
        abort(400, "Unknown status")
    from ..services import audit_service
    audit_service.log("ACTION", "Incident", i.incident_no, "incident_status", {"from": i.status, "to": st, "note": request.form.get("note")}, actor=H.actor())
    i.status = st
    if request.form.get("cascade"):
        for a in i.alerts:
            if a.status in alert_service.OPEN_STATUSES:
                a.status = st if st in alert_service.STATUSES else a.status
    db.session.commit()
    H.ok(f"Incident {i.incident_no} → {st}.")
    return H.back("alerts.incidents")
