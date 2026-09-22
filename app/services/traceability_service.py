"""Lot / batch / serial traceability: backward (where did it come from?) and forward (where did it go?)."""
from __future__ import annotations

from ..models import Customer, InventoryBalance, InventoryTransaction, Item, Location, Lot, SerialNumber, Supplier

STAGE = {"RECEIPT": "Receipt", "PROD_COMPLETION": "Production", "PROD_CONSUMPTION": "Consumption", "TRANSFER": "Movement", "SHIPMENT": "Customer",
         "RETURN": "Return", "QUARANTINE": "Quality", "RELEASE": "Quality", "SCRAP": "Disposal", "ISSUE": "Issue", "OPENING": "Opening balance",
         "ADJUSTMENT": "Adjustment", "CYCLE_COUNT": "Count"}


def find_lot(q: str) -> Lot | None:
    q = (q or "").strip()
    if not q:
        return None
    return Lot.query.filter(Lot.lot_no == q).first() or Lot.query.filter(Lot.lot_no.ilike(f"%{q}%")).first()


def _events(lot: Lot):
    locs = {l.id: l.code for l in Location.query.all()}
    custs = {c.id: c.name for c in Customer.query.all()}
    sups = {s.id: s.name for s in Supplier.query.all()}
    rows = []
    for t in InventoryTransaction.query.filter_by(lot_id=lot.id).order_by(InventoryTransaction.occurred_at, InventoryTransaction.id).all():
        where = locs.get(t.location_id)
        if t.to_location_id:
            where = f"{where} → {locs.get(t.to_location_id)}"
        rows.append({"when": t.occurred_at, "stage": STAGE.get(t.txn_type, t.txn_type), "type": t.txn_type, "qty": t.quantity, "where": where,
                     "ref": f"{t.ref_type or ''} {t.ref_id or ''}".strip(), "party": custs.get(t.customer_id) or sups.get(t.supplier_id), "reason": t.reason})
    return rows


def trace(lot: Lot, depth: int = 0, seen: set | None = None) -> dict:
    """Full genealogy for a lot: its own events, upstream inputs (via the production order that made it) and downstream lots/customers."""
    seen = seen or set()
    seen.add(lot.id)
    item = Item.query.get(lot.item_id)
    sup = Supplier.query.get(lot.supplier_id) if lot.supplier_id else None
    events = _events(lot)
    stock = [{"location": b.location.code, "state": b.state, "qty": b.quantity} for b in InventoryBalance.query.filter_by(lot_id=lot.id).all() if b.quantity]
    upstream, downstream = [], []
    made_by = [t for t in InventoryTransaction.query.filter_by(lot_id=lot.id, txn_type="PROD_COMPLETION").all()]
    used_in = [t for t in InventoryTransaction.query.filter_by(lot_id=lot.id, txn_type="PROD_CONSUMPTION").all()]
    if depth < 3:
        for t in made_by:
            for c in InventoryTransaction.query.filter(InventoryTransaction.ref_id == t.ref_id, InventoryTransaction.txn_type == "PROD_CONSUMPTION").all():
                if c.lot_id and c.lot_id not in seen:
                    upstream.append(trace(Lot.query.get(c.lot_id), depth + 1, seen))
        for t in used_in:
            for c in InventoryTransaction.query.filter(InventoryTransaction.ref_id == t.ref_id, InventoryTransaction.txn_type == "PROD_COMPLETION").all():
                if c.lot_id and c.lot_id not in seen:
                    downstream.append(trace(Lot.query.get(c.lot_id), depth + 1, seen))
    customers = sorted({e["party"] for e in events if e["type"] == "SHIPMENT" and e["party"]})
    for d in downstream:
        customers = sorted(set(customers) | set(d["customers"]))
    chain = ["Supplier" if sup else None, "Receipt" if any(e["type"] == "RECEIPT" for e in events) else None, "Warehouse" if stock or any(e["type"] == "TRANSFER" for e in events) else None,
             "Production" if (made_by or used_in) else None, "Customer" if customers else None]
    serials = [s.serial_no for s in SerialNumber.query.filter_by(lot_id=lot.id).limit(20).all()]
    return {"lot": lot.lot_no, "lot_id": lot.id, "sku": item.sku if item else None, "description": item.description if item else None, "supplier": sup.name if sup else None,
            "quality": lot.quality_status, "manufactured": lot.manufacture_date, "received": lot.received_date, "expiry": lot.expiry_date, "events": events, "stock": stock,
            "upstream": upstream, "downstream": downstream, "customers": customers, "chain": [c for c in chain if c], "serials": serials}
