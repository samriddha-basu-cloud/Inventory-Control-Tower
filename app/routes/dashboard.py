from flask import Blueprint, render_template
from app.models import Item, Location, Alert, Incident, Recommendation
from app.services import inventory_service, financial_service, alert_service

bp = Blueprint("dashboard", __name__)


@bp.route("/dashboard")
def control_tower():
    has_data = Item.query.first() is not None
    if not has_data:
        return render_template("dashboard/empty.html")

    kpis = inventory_service.network_kpis()
    financials = financial_service.network_financials()
    health = financial_service.network_health_index()
    priority_counts = alert_service.priority_counts()

    top_alerts = Alert.query.filter(Alert.status.notin_(["RESOLVED", "CLOSED"])).order_by(
        Alert.severity, Alert.financial_impact.desc()
    ).limit(8).all()
    incidents = Incident.query.filter_by(status="OPEN").order_by(Incident.financial_impact.desc()).limit(5).all()
    pending_recs = Recommendation.query.filter_by(status="PENDING").order_by(
        Recommendation.cost_estimate.desc()
    ).limit(6).all()

    return render_template(
        "dashboard/control_tower.html", kpis=kpis, financials=financials, health=health,
        priority_counts=priority_counts, top_alerts=top_alerts, incidents=incidents,
        pending_recs=pending_recs, node_count=Location.query.count(), sku_count=Item.query.count(),
    )
