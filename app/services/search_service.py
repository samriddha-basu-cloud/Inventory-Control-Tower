"""Global search across SKU, PO, SO, shipment, supplier, location, batch/lot and incident with direct navigation URLs."""
from __future__ import annotations

from flask import url_for

from ..models import Alert, Incident, Item, Location, Lot, PurchaseOrder, SalesOrder, Shipment, Supplier


def _like(col, q):
    return col.ilike(f"%{q}%")


def search(q: str, limit: int = 6) -> list[dict]:
    q = (q or "").strip()
    if len(q) < 2:
        return []
    out = []
    for i in Item.query.filter(_like(Item.sku, q) | _like(Item.description, q)).limit(limit).all():
        out.append({"type": "SKU", "label": i.sku, "sub": i.description, "url": url_for("inventory.sku360", sku=i.sku)})
    for l in Location.query.filter(_like(Location.code, q) | _like(Location.name, q)).limit(limit).all():
        out.append({"type": "Location", "label": l.code, "sub": l.name, "url": url_for("inventory.location360", code=l.code)})
    for s in Supplier.query.filter(_like(Supplier.code, q) | _like(Supplier.name, q)).limit(limit).all():
        out.append({"type": "Supplier", "label": s.code, "sub": s.name, "url": url_for("inventory.supplier360", code=s.code)})
    for p in PurchaseOrder.query.filter(_like(PurchaseOrder.po_number, q)).limit(limit).all():
        out.append({"type": "PO", "label": p.po_number, "sub": f"{p.status} · ETA {p.eta_date}", "url": url_for("risk.supply", q=p.po_number)})
    for s in SalesOrder.query.filter(_like(SalesOrder.so_number, q)).limit(limit).all():
        out.append({"type": "SO", "label": s.so_number, "sub": f"{s.status} · due {s.requested_date}", "url": url_for("risk.pegging", q=s.so_number)})
    for s in Shipment.query.filter(_like(Shipment.shipment_no, q) | _like(Shipment.tracking, q)).limit(limit).all():
        out.append({"type": "Shipment", "label": s.shipment_no, "sub": f"{s.status} · {s.lane}", "url": url_for("risk.supply", q=s.shipment_no)})
    for l in Lot.query.filter(_like(Lot.lot_no, q)).limit(limit).all():
        out.append({"type": "Batch/Lot", "label": l.lot_no, "sub": f"{l.item.sku if l.item else ''} · {l.quality_status}", "url": url_for("inventory.trace", lot=l.lot_no)})
    for i in Incident.query.filter(_like(Incident.title, q) | _like(Incident.incident_no, q)).limit(limit).all():
        out.append({"type": "Incident", "label": i.incident_no, "sub": i.title, "url": url_for("alerts.incident_detail", incident_id=i.id)})
    for a in Alert.query.filter(_like(Alert.alert_no, q) | _like(Alert.title, q)).limit(3).all():
        out.append({"type": "Alert", "label": a.alert_no, "sub": a.title, "url": url_for("alerts.alert_detail", alert_id=a.id)})
    return out
