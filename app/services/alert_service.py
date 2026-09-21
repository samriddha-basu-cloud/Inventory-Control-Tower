"""
Alert Engine — generation, prioritization, clustering (sections 67-70).

Alerts are derived purely from actual ledger/demand/policy/PO data already
in the database; nothing here fabricates a root cause. Where a cause cannot
be confirmed from data it is labeled "Likely cause" or "Unconfirmed cause",
never stated as fact (section 179).
"""
from datetime import date, datetime, timedelta

from app.extensions import db
from app.models import (Alert, Incident, Item, Location, InventoryLedger, SafetyStockPolicy,
                         PurchaseOrder, Supplier)
from app.services import risk_service, inventory_service
from app.optimization.reorder_point import reorder_point as calc_rop


def _severity_from_impact(financial_impact, service_impact_pct):
    if financial_impact >= 500_000 or service_impact_pct >= 20:
        return "P1"
    if financial_impact >= 150_000 or service_impact_pct >= 10:
        return "P2"
    if financial_impact >= 25_000 or service_impact_pct >= 3:
        return "P3"
    return "P4"


def generate_alerts(clear_existing=True):
    if clear_existing:
        Alert.query.delete()
        Incident.query.filter(Incident.title.like("Auto-generated:%")).delete(synchronize_session=False)
        db.session.commit()

    new_alerts = []
    combos = (
        db.session.query(InventoryLedger.item_id, InventoryLedger.location_id)
        .distinct()
        .all()
    )

    for item_id, location_id in combos:
        item = Item.query.get(item_id)
        location = Location.query.get(location_id)
        if not item or not location:
            continue

        # --- Stockout risk --------------------------------------------------
        risk = risk_service.stockout_risk(item_id, location_id)
        if risk["probability_pct"] >= 30:
            sev = _severity_from_impact(risk["revenue_at_risk"], risk["probability_pct"])
            new_alerts.append(Alert(
                alert_type="stockout_risk", item_id=item_id, location_id=location_id,
                severity=sev, financial_impact=risk["revenue_at_risk"],
                service_impact_pct=risk["probability_pct"],
                message=(f"{item.sku} at {location.code}: {risk['probability_pct']}% stockout probability "
                         f"within lead time. Projected stockout in {risk['days_to_stockout']} days "
                         f"(₹{risk['revenue_at_risk']:,.0f} revenue at risk)."),
            ))

        # --- Safety stock / ROP breach ---------------------------------------
        policy = SafetyStockPolicy.query.filter_by(item_id=item_id, location_id=location_id).first()
        avail = inventory_service.available_quantity(item_id, location_id)
        if policy and avail < policy.effective_safety_stock:
            shortfall = policy.effective_safety_stock - avail
            new_alerts.append(Alert(
                alert_type="safety_stock_breach", item_id=item_id, location_id=location_id,
                severity=_severity_from_impact(shortfall * item.unit_cost, 5),
                financial_impact=round(shortfall * item.unit_cost, 2), service_impact_pct=5.0,
                message=(f"{item.sku} at {location.code}: available ({avail:.0f}) is below "
                         f"safety stock ({policy.effective_safety_stock:.0f})."),
            ))

        # --- Excess ------------------------------------------------------------
        excess = risk_service.excess_detection(item_id, location_id)
        if excess["is_excess"]:
            new_alerts.append(Alert(
                alert_type="excess", item_id=item_id, location_id=location_id,
                severity=_severity_from_impact(excess["excess_value"], 0),
                financial_impact=excess["excess_value"], service_impact_pct=0.0,
                message=(f"{item.sku} at {location.code}: {excess['excess_quantity']:.0f} units "
                         f"(₹{excess['excess_value']:,.0f}) above target stock "
                         f"({excess['days_of_supply']} days of supply)."),
            ))
            if excess["classification"] == "OBSOLETE_CANDIDATE":
                new_alerts.append(Alert(
                    alert_type="obsolescence", item_id=item_id, location_id=location_id,
                    severity="P3", financial_impact=excess["excess_value"], service_impact_pct=0.0,
                    message=(f"{item.sku} at {location.code}: no demand for "
                             f"{excess['days_since_last_txn']} days - obsolescence candidate."),
                ))

        # --- Expiry --------------------------------------------------------
        near_expiry_rows = InventoryLedger.query.filter(
            InventoryLedger.item_id == item_id, InventoryLedger.location_id == location_id,
            InventoryLedger.expiry_date.isnot(None),
        ).all()
        for row in near_expiry_rows:
            if row.expiry_date is None:
                continue
            remaining = (row.expiry_date - date.today()).days
            if remaining < 0:
                new_alerts.append(Alert(
                    alert_type="expiry", item_id=item_id, location_id=location_id, severity="P2",
                    financial_impact=round(row.quantity * item.unit_cost, 2), service_impact_pct=0.0,
                    message=f"{item.sku} batch {row.batch_code} at {location.code} EXPIRED "
                            f"{abs(remaining)} days ago ({row.quantity:.0f} units).",
                ))
            elif remaining <= 30:
                new_alerts.append(Alert(
                    alert_type="expiry", item_id=item_id, location_id=location_id, severity="P3",
                    financial_impact=round(row.quantity * item.unit_cost, 2), service_impact_pct=0.0,
                    message=f"{item.sku} batch {row.batch_code} at {location.code} expires in "
                            f"{remaining} days ({row.quantity:.0f} units).",
                ))

    # --- Late POs (supplier delay) --------------------------------------------
    late_pos = PurchaseOrder.query.filter(
        PurchaseOrder.status.in_(["OPEN", "IN_TRANSIT", "DELAYED"]),
        PurchaseOrder.current_eta.isnot(None), PurchaseOrder.original_eta.isnot(None),
        PurchaseOrder.current_eta > PurchaseOrder.original_eta,
    ).all()
    incident = None
    delayed_group = [po for po in late_pos if (po.current_eta - po.original_eta).days >= 3]
    if delayed_group:
        total_impact = 0.0
        affected_skus = set()
        affected_locations = set()
        for po in delayed_group:
            for line in po.lines:
                impact = line.quantity_ordered * (line.item.unit_price if line.item else 0)
                total_impact += impact
                affected_skus.add(line.item_id)
                affected_locations.add(po.destination_location_id)
        supplier_names = sorted({po.supplier.name for po in delayed_group if po.supplier})
        incident = Incident(
            title=f"Auto-generated: Supplier delay ({', '.join(supplier_names[:2])})",
            root_cause_observed=(f"{len(delayed_group)} purchase order(s) from "
                                  f"{', '.join(supplier_names)} are running late "
                                  f"vs. original ETA."),
            root_cause_likely="Upstream supplier or transportation delay (unconfirmed root cause "
                               "beyond the observed ETA slip).",
            affected_sku_count=len(affected_skus), affected_location_count=len(affected_locations),
            financial_impact=round(total_impact, 2), status="OPEN",
        )
        db.session.add(incident)
        db.session.flush()

        for po in delayed_group:
            delay_days = (po.current_eta - po.original_eta).days
            new_alerts.append(Alert(
                alert_type="late_po", item_id=po.lines[0].item_id if po.lines else None,
                location_id=po.destination_location_id,
                severity=_severity_from_impact(50000 * len(po.lines), delay_days),
                financial_impact=round(sum(l.quantity_ordered * (l.item.unit_price if l.item else 0)
                                            for l in po.lines), 2),
                service_impact_pct=min(delay_days * 2, 40),
                message=(f"PO {po.po_number} from {po.supplier.name if po.supplier else 'supplier'} "
                         f"delayed {delay_days} days (ETA {po.current_eta})."),
                incident_id=incident.id,
            ))

    db.session.add_all(new_alerts)
    db.session.commit()
    return {"alerts_generated": len(new_alerts), "incidents_created": 1 if incident else 0}


def cluster_summary():
    """Group open alerts by incident for the exception workbench (section 69)."""
    incidents = Incident.query.filter_by(status="OPEN").order_by(Incident.financial_impact.desc()).all()
    return incidents


def priority_counts():
    counts = {"P1": 0, "P2": 0, "P3": 0, "P4": 0}
    for sev, cnt in db.session.query(Alert.severity, db.func.count(Alert.id)).group_by(Alert.severity).all():
        counts[sev] = cnt
    return counts
