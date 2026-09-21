import json
from flask import Blueprint, render_template, request, redirect, url_for, flash

from app.extensions import db
from app.models import Alert, Incident, Recommendation, Approval, Execution
from app.services import alert_service

bp = Blueprint("alerts", __name__, url_prefix="/exceptions")


@bp.route("/")
def workbench():
    severity = request.args.get("severity")
    alert_type = request.args.get("type")
    status = request.args.get("status")
    q = Alert.query
    if severity:
        q = q.filter_by(severity=severity)
    if alert_type:
        q = q.filter_by(alert_type=alert_type)
    if status:
        q = q.filter_by(status=status)
    else:
        q = q.filter(Alert.status.notin_(["CLOSED"]))
    alerts = q.order_by(Alert.severity, Alert.financial_impact.desc()).limit(200).all()
    counts = alert_service.priority_counts()
    return render_template("alerts/workbench.html", alerts=alerts, counts=counts,
                            severity=severity, alert_type=alert_type, status=status)


@bp.route("/generate", methods=["POST"])
def generate():
    result = alert_service.generate_alerts()
    flash(f"{result['alerts_generated']} alerts generated, {result['incidents_created']} incident(s) created.",
          "success")
    return redirect(url_for("alerts.workbench"))


@bp.route("/incidents")
def incidents():
    rows = Incident.query.order_by(Incident.financial_impact.desc()).all()
    return render_template("alerts/incidents.html", incidents=rows)


@bp.route("/incidents/<int:incident_id>")
def incident_detail(incident_id):
    incident = Incident.query.get_or_404(incident_id)
    related_alerts = Alert.query.filter_by(incident_id=incident.id).all()
    return render_template("alerts/incident_detail.html", incident=incident, related_alerts=related_alerts)


@bp.route("/alerts/<int:alert_id>/status", methods=["POST"])
def update_alert_status(alert_id):
    alert = Alert.query.get_or_404(alert_id)
    new_status = request.form.get("status")
    if new_status in ("NEW", "INVESTIGATING", "ACTION_REQUIRED", "APPROVED", "EXECUTING", "RESOLVED", "CLOSED"):
        alert.status = new_status
        from datetime import datetime
        if new_status in ("RESOLVED", "CLOSED"):
            alert.resolved_at = datetime.utcnow()
        db.session.commit()
        flash(f"Alert #{alert.id} marked {new_status}.", "success")
    return redirect(url_for("alerts.workbench"))


# --- Recommendations / approval workflow (sections 80-86) ---------------------

@bp.route("/recommendations")
def recommendations():
    status = request.args.get("status", "PENDING")
    q = Recommendation.query
    if status:
        q = q.filter_by(status=status)
    recs = q.order_by(Recommendation.cost_estimate.desc()).all()
    for r in recs:
        r.reason = json.loads(r.reason_json) if r.reason_json else {}
        r.expected_impact = json.loads(r.expected_impact_json) if r.expected_impact_json else {}
    return render_template("alerts/recommendations.html", recs=recs, status=status)


@bp.route("/recommendations/<int:rec_id>/decide", methods=["POST"])
def decide_recommendation(rec_id):
    rec = Recommendation.query.get_or_404(rec_id)
    decision = request.form.get("decision")  # APPROVE|REJECT|ESCALATE
    reason = request.form.get("reason", "")
    decided_by = request.form.get("decided_by", "planner@demo.org")

    db.session.add(Approval(recommendation_id=rec.id, decision=decision, reason=reason, decided_by=decided_by))

    if decision == "APPROVE":
        rec.status = "APPROVED"
        # Guardrail: autonomy level 3 (auto-execute) only below a configured threshold.
        auto_threshold = 50000
        mode = "SIMULATED"
        if rec.cost_estimate is not None and rec.cost_estimate <= auto_threshold and rec.autonomy_level >= 3:
            result = "SUCCESS"
            detail = "Auto-executed within guardrail threshold (simulation mode - no live ERP/WMS connector configured)."
        else:
            result = "PENDING_EXECUTION"
            detail = "Approved. Awaiting execution step (simulation mode)."
        db.session.add(Execution(recommendation_id=rec.id, mode=mode, result=result, detail=detail))
        rec.status = "EXECUTED" if result == "SUCCESS" else "APPROVED"
    elif decision == "REJECT":
        rec.status = "REJECTED"
    elif decision == "MODIFY":
        rec.status = "MODIFIED"
    else:
        rec.status = "PENDING"

    db.session.commit()
    flash(f"Recommendation #{rec.id}: {decision}.", "success")
    return redirect(url_for("alerts.recommendations"))


@bp.route("/recommendations/<int:rec_id>/execute", methods=["POST"])
def execute_recommendation(rec_id):
    rec = Recommendation.query.get_or_404(rec_id)
    if rec.status != "APPROVED":
        flash("Only approved recommendations can be executed.", "warning")
        return redirect(url_for("alerts.recommendations"))

    # Execution safety validation (section 147) - re-check supply availability before executing.
    from app.services import inventory_service
    blocked_reason = None
    if rec.rec_type == "transfer" and rec.source_location_id:
        avail = inventory_service.available_quantity(rec.item_id, rec.source_location_id)
        if rec.quantity and avail < rec.quantity:
            blocked_reason = (f"Source location availability ({avail:.0f}) has fallen below the "
                               f"recommended transfer quantity ({rec.quantity:.0f}) since approval.")

    if blocked_reason:
        db.session.add(Execution(recommendation_id=rec.id, mode="SIMULATED", result="BLOCKED", detail=blocked_reason))
        db.session.commit()
        flash(f"Execution blocked: {blocked_reason}", "danger")
    else:
        db.session.add(Execution(recommendation_id=rec.id, mode="SIMULATED", result="SUCCESS",
                                  detail="Simulated execution completed (no live ERP/WMS/TMS connector configured)."))
        rec.status = "EXECUTED"
        db.session.commit()
        flash(f"Recommendation #{rec.id} executed (simulation mode).", "success")
    return redirect(url_for("alerts.recommendations"))
