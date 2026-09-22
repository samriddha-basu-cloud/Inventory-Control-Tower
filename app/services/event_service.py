"""Event-driven architecture: canonical event schemas, validation, idempotent synchronous processing.

Events are validated against EVENT_SCHEMAS, persisted (Event table, unique event_id ⇒ replay-safe) and dispatched to
handlers in-process. `EventPublisher` is the seam for a future Kafka / Azure Event Hub transport: swap `BUS.publisher`
for an implementation that produces to a topic and consume the same payloads with the same `dispatch()`.
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import date, datetime

from ..extensions import db
from ..models import (Alert, Demand, Event, Forecast, Item, Location, Lot, PurchaseOrder, PurchaseOrderLine, Shipment, ShipmentLine, Supplier, TransferOrder)
from ..models.base import utcnow
from . import audit_service as audit
from . import ledger_service as ledger
from . import settings_service as S

# field -> (type, required)
EVENT_SCHEMAS: dict[str, dict] = {
    "InventoryReceived": {"sku": (str, True), "location": (str, True), "qty": (float, True), "lot": (str, False), "po": (str, False), "txn_id": (str, False)},
    "InventoryIssued": {"sku": (str, True), "location": (str, True), "qty": (float, True), "lot": (str, False), "ref": (str, False), "txn_id": (str, False)},
    "ShipmentDispatched": {"shipment_no": (str, True), "po": (str, False), "eta": (str, False), "lines": (list, False), "carrier": (str, False), "mode": (str, False)},
    "ShipmentDelayed": {"shipment_no": (str, True), "eta": (str, False), "reason": (str, False), "delay_days": (float, False)},
    "PurchaseOrderCreated": {"po_number": (str, True), "supplier": (str, True), "location": (str, True), "sku": (str, True), "qty": (float, True),
                             "promised_date": (str, False)},
    "PurchaseOrderDelayed": {"po_number": (str, True), "new_promised_date": (str, False), "reason": (str, False)},
    "DemandChanged": {"sku": (str, True), "location": (str, True), "period_start": (str, True), "qty": (float, True)},
    "ForecastUpdated": {"sku": (str, True), "location": (str, True), "period_start": (str, True), "qty": (float, True), "forecast_type": (str, False), "source": (str, False)},
    "InventoryAdjusted": {"sku": (str, True), "location": (str, True), "counted_qty": (float, True), "reason": (str, False)},
    "TransferCreated": {"to_number": (str, True), "sku": (str, True), "from_location": (str, True), "to_location": (str, True), "qty": (float, True), "eta": (str, False)},
    "TransferCompleted": {"to_number": (str, True), "qty": (float, False)},
    "QualityHold": {"sku": (str, True), "location": (str, True), "lot": (str, True), "qty": (float, True), "reason": (str, False)},
    "QualityRelease": {"sku": (str, True), "location": (str, True), "lot": (str, True), "qty": (float, True)},
    "ExpiryApproaching": {"sku": (str, True), "location": (str, False), "lot": (str, True), "days_to_expiry": (float, True)},
    "ShipmentStatus": {"shipment_no": (str, True), "status": (str, False)},   # extension used by EDI 214 translation
}


class EventError(ValueError):
    pass


def validate(event_type: str, payload: dict) -> dict:
    schema = EVENT_SCHEMAS.get(event_type)
    if schema is None:
        raise EventError(f"Unknown event type '{event_type}'. Known: {', '.join(sorted(EVENT_SCHEMAS))}")
    if not isinstance(payload, dict):
        raise EventError("Event payload must be an object")
    clean = {}
    for f, (typ, req) in schema.items():
        v = payload.get(f)
        if v in (None, ""):
            if req:
                raise EventError(f"{event_type}: missing required field '{f}'")
            continue
        try:
            clean[f] = typ(v) if typ in (float, str) else v
        except (TypeError, ValueError) as e:
            raise EventError(f"{event_type}: field '{f}' must be {typ.__name__}") from e
    if "qty" in clean and clean["qty"] < 0 or "counted_qty" in clean and clean["counted_qty"] < 0:
        raise EventError(f"{event_type}: quantity cannot be negative")
    return {**payload, **clean}


class EventPublisher(ABC):
    @abstractmethod
    def publish(self, event: Event) -> None: ...


class InProcessPublisher(EventPublisher):
    """Default transport: dispatch synchronously. A Kafka/Event Hub publisher would produce to a topic instead."""

    def publish(self, event: Event) -> None:
        dispatch(event)


class KafkaPublisher(EventPublisher):  # pragma: no cover - architecture placeholder
    def publish(self, event: Event) -> None:
        raise NotImplementedError("Kafka/Event Hub transport is not configured. Provide bootstrap servers and a producer implementation.")


class Bus:
    publisher: EventPublisher = InProcessPublisher()


BUS = Bus()


def publish(event_type: str, payload: dict, source: str = "API", event_id: str | None = None, occurred_at: datetime | None = None) -> Event:
    event_id = event_id or uuid.uuid4().hex
    existing = Event.query.filter_by(event_id=event_id).first()
    if existing:
        existing.status = existing.status if existing.status == "PROCESSED" else "DUPLICATE"
        return existing
    ev = Event(event_id=event_id, event_type=event_type, source=source, occurred_at=occurred_at or S.now(), payload=payload, status="RECEIVED")
    try:
        ev.payload = validate(event_type, payload)
    except EventError as e:
        ev.status, ev.error = "FAILED", str(e)[:390]
        db.session.add(ev)
        db.session.flush()
        return ev
    db.session.add(ev)
    db.session.flush()
    BUS.publisher.publish(ev)
    return ev


def dispatch(ev: Event) -> None:
    h = HANDLERS.get(ev.event_type)
    try:
        with db.session.begin_nested():
            if h:
                h(ev.payload, ev)
        ev.status = "PROCESSED"
    except Exception as e:
        ev.status, ev.error = "FAILED", str(e)[:390]
    S.bump_version()
    audit.log("DATA", "Event", ev.event_id, f"event_{ev.status.lower()}", {"type": ev.event_type, "source": ev.source, "error": ev.error})


# ---------------------------------------------------------------------------------------------------------- handlers
def _item(sku):
    it = Item.query.filter_by(sku=sku).first()
    if not it:
        raise EventError(f"Missing SKU '{sku}'")
    return it


def _loc(code):
    l = Location.query.filter_by(code=code).first()
    if not l:
        raise EventError(f"Unknown location '{code}'")
    return l


def _lot(item, no):
    if not no:
        return None
    l = Lot.query.filter_by(item_id=item.id, lot_no=no).first()
    if not l:
        l = Lot(item_id=item.id, lot_no=no, received_date=S.today(), quality_status="RELEASED", source_system="EVENT")
        db.session.add(l)
        db.session.flush()
    return l


def _d(v):
    return date.fromisoformat(v[:10]) if v else None


def h_received(p, ev):
    it, loc = _item(p["sku"]), _loc(p["location"])
    lot = _lot(it, p.get("lot"))
    ledger.post("RECEIPT", it.id, loc.id, p["qty"], lot_id=lot.id if lot else None, ref_type="PO", ref_id=p.get("po"), txn_id=p.get("txn_id") or f"EV-{ev.event_id}",
                reason="InventoryReceived event", source_system=ev.source)
    if p.get("po"):
        po = PurchaseOrder.query.filter_by(po_number=p["po"]).first()
        if po:
            for ln in po.lines:
                if ln.item_id == it.id:
                    ln.qty_received = (ln.qty_received or 0) + p["qty"]
                    ln.qty_in_transit = max(0.0, (ln.qty_in_transit or 0) - p["qty"])
                    if ln.qty_received >= ln.qty_ordered - 1e-9:
                        ln.status = "RECEIVED"
            if all((l.qty_received or 0) >= l.qty_ordered - 1e-9 for l in po.lines):
                po.status, po.actual_date = "RECEIVED", S.today()
            else:
                po.status = "PARTIAL"


def h_issued(p, ev):
    it, loc = _item(p["sku"]), _loc(p["location"])
    lot = _lot(it, p.get("lot"))
    ledger.post("ISSUE", it.id, loc.id, p["qty"], lot_id=lot.id if lot else None, ref_type="ISSUE", ref_id=p.get("ref"), txn_id=p.get("txn_id") or f"EV-{ev.event_id}",
                reason="InventoryIssued event", source_system=ev.source)


def h_ship_dispatched(p, ev):
    s = Shipment.query.filter_by(shipment_no=p["shipment_no"]).first()
    if not s:
        s = Shipment(shipment_no=p["shipment_no"], ref_type="PO" if p.get("po") else None, ref_number=p.get("po"), mode=p.get("mode") or "ROAD", status="IN_TRANSIT",
                     dispatch_date=S.today(), source_system=ev.source)
        db.session.add(s)
        db.session.flush()
    if p.get("eta"):
        s.eta_date = _d(p["eta"])
    s.status = "IN_TRANSIT"
    po = PurchaseOrder.query.filter_by(po_number=p.get("po")).first() if p.get("po") else None
    if po:
        s.supplier_id, s.dest_location_id = po.supplier_id, po.dest_location_id
        s.promised_date = s.promised_date or po.promised_date
    for ln in p.get("lines") or []:
        it = Item.query.filter_by(sku=ln.get("sku")).first()
        if not it:
            continue
        db.session.add(ShipmentLine(shipment_id=s.id, item_id=it.id, qty=float(ln.get("qty", 0))))
        if po:
            for pl in po.lines:
                if pl.item_id == it.id:
                    pl.qty_in_transit = min(pl.qty_ordered - (pl.qty_received or 0), (pl.qty_in_transit or 0) + float(ln["qty"]))
                    if p.get("eta"):
                        pl.eta_date = _d(p["eta"])


def h_ship_delayed(p, ev):
    s = Shipment.query.filter_by(shipment_no=p["shipment_no"]).first()
    if not s:
        raise EventError(f"Unknown shipment '{p['shipment_no']}'")
    old = s.eta_date
    if p.get("eta"):
        s.eta_date = _d(p["eta"])
    s.delay_days = p.get("delay_days") or ((s.eta_date - s.promised_date).days if (s.eta_date and s.promised_date) else (s.delay_days or 0))
    s.delay_reason = p.get("reason") or s.delay_reason
    s.status = "DELAYED"
    if s.ref_number:
        po = PurchaseOrder.query.filter_by(po_number=s.ref_number).first()
        if po:
            po.eta_date, po.delay_reason = s.eta_date, s.delay_reason
            for pl in po.lines:
                pl.eta_date = s.eta_date


def h_ship_status(p, ev):
    s = Shipment.query.filter_by(shipment_no=p["shipment_no"]).first()
    if s and p.get("status") == "DELIVERED":
        s.status, s.actual_date = "DELIVERED", S.today()


def h_po_created(p, ev):
    sup = Supplier.query.filter_by(code=p["supplier"]).first()
    if not sup:
        raise EventError(f"Unknown supplier '{p['supplier']}'")
    it, loc = _item(p["sku"]), _loc(p["location"])
    if PurchaseOrder.query.filter_by(po_number=p["po_number"]).first():
        raise EventError(f"Duplicate purchase order {p['po_number']}")
    prom = _d(p.get("promised_date")) or S.today()
    po = PurchaseOrder(po_number=p["po_number"], supplier_id=sup.id, dest_location_id=loc.id, order_date=S.today(), promised_date=prom, eta_date=prom, status="OPEN",
                       source_system=ev.source)
    db.session.add(po)
    db.session.flush()
    db.session.add(PurchaseOrderLine(po_id=po.id, line_no=1, item_id=it.id, qty_ordered=p["qty"], promised_date=prom, eta_date=prom, source_system=ev.source))


def h_po_delayed(p, ev):
    po = PurchaseOrder.query.filter_by(po_number=p["po_number"]).first()
    if not po:
        raise EventError(f"Unknown PO '{p['po_number']}'")
    if p.get("new_promised_date"):
        po.eta_date = _d(p["new_promised_date"])
        for l in po.lines:
            l.eta_date = po.eta_date
    po.delay_reason = p.get("reason") or po.delay_reason


def h_demand(p, ev):
    it, loc = _item(p["sku"]), _loc(p["location"])
    db.session.add(Demand(item_id=it.id, location_id=loc.id, period_start=_d(p["period_start"]), granularity="W", qty=p["qty"], source_system=ev.source))


def h_forecast(p, ev):
    it, loc = _item(p["sku"]), _loc(p["location"])
    ps = _d(p["period_start"])
    ft = (p.get("forecast_type") or "BASELINE").upper()
    row = Forecast.query.filter_by(item_id=it.id, location_id=loc.id, period_start=ps, forecast_type=ft).first()
    if row:
        row.qty, row.source, row.issued_at = p["qty"], p.get("source") or row.source, S.today()
    else:
        db.session.add(Forecast(item_id=it.id, location_id=loc.id, period_start=ps, granularity="W", qty=p["qty"], forecast_type=ft, source=p.get("source") or "EVENT",
                                issued_at=S.today(), source_system=ev.source))


def h_adjusted(p, ev):
    it, loc = _item(p["sku"]), _loc(p["location"])
    ledger.cycle_count(it.id, loc.id, p["counted_qty"], reason=p.get("reason") or "InventoryAdjusted event")


def h_transfer_created(p, ev):
    it, a, b = _item(p["sku"]), _loc(p["from_location"]), _loc(p["to_location"])
    eta = _d(p.get("eta")) or S.today()
    db.session.add(TransferOrder(to_number=p["to_number"], item_id=it.id, from_location_id=a.id, to_location_id=b.id, qty=p["qty"], ship_date=S.today(), eta_date=eta,
                                 promised_date=eta, status="OPEN", source_system=ev.source))


def h_transfer_completed(p, ev):
    t = TransferOrder.query.filter_by(to_number=p["to_number"]).first()
    if not t:
        raise EventError(f"Unknown transfer '{p['to_number']}'")
    qty = p.get("qty") or t.qty
    try:
        ledger.post("TRANSFER", t.item_id, t.from_location_id, qty, to_location_id=t.to_location_id, lot_id=t.lot_id, ref_type="TO", ref_id=t.to_number,
                    reason="TransferCompleted event", txn_id=f"EV-{ev.event_id}")
    except ledger.NegativeInventoryError as e:
        raise EventError(str(e)) from e
    t.qty_received, t.status, t.actual_date = qty, "RECEIVED", S.today()


def h_quality_hold(p, ev):
    it, loc = _item(p["sku"]), _loc(p["location"])
    lot = _lot(it, p["lot"])
    ledger.post("QUARANTINE", it.id, loc.id, p["qty"], lot_id=lot.id, reason=p.get("reason") or "QualityHold event", txn_id=f"EV-{ev.event_id}")
    lot.quality_status = "QUARANTINE"


def h_quality_release(p, ev):
    it, loc = _item(p["sku"]), _loc(p["location"])
    lot = _lot(it, p["lot"])
    ledger.post("RELEASE", it.id, loc.id, p["qty"], lot_id=lot.id, reason="QualityRelease event", txn_id=f"EV-{ev.event_id}")
    lot.quality_status = "RELEASED"


def h_expiry(p, ev):
    it = _item(p["sku"])
    key = f"EVENT_EXPIRY|{p['lot']}"
    if not Alert.query.filter_by(dedupe_key=key).first():
        db.session.add(Alert(dedupe_key=key, rule_code="EVENT_EXPIRY", alert_type="EXPIRY", severity="HIGH" if p["days_to_expiry"] <= 30 else "MEDIUM", status="New",
                             item_id=it.id, title=f"Expiry approaching: lot {p['lot']} ({it.sku})", message=f"{p['days_to_expiry']:.0f} days to expiry (external event)",
                             dims={"sku": it.sku, "lot": [p["lot"]]}, impact={}, owner="Warehouse Manager", recommended_action="REVIEW_EXPIRY"))


HANDLERS = {"InventoryReceived": h_received, "InventoryIssued": h_issued, "ShipmentDispatched": h_ship_dispatched, "ShipmentDelayed": h_ship_delayed,
            "ShipmentStatus": h_ship_status, "PurchaseOrderCreated": h_po_created, "PurchaseOrderDelayed": h_po_delayed, "DemandChanged": h_demand,
            "ForecastUpdated": h_forecast, "InventoryAdjusted": h_adjusted, "TransferCreated": h_transfer_created, "TransferCompleted": h_transfer_completed,
            "QualityHold": h_quality_hold, "QualityRelease": h_quality_release, "ExpiryApproaching": h_expiry}
