"""Supply and demand documents: PO, SO, TO, production orders, shipments."""
from ..extensions import db
from .base import CanonicalMixin, utcnow


class PurchaseOrder(CanonicalMixin, db.Model):
    __tablename__ = "purchase_order"
    id = db.Column(db.Integer, primary_key=True)
    po_number = db.Column(db.String(40), unique=True, nullable=False, index=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"), index=True)
    dest_location_id = db.Column(db.Integer, db.ForeignKey("location.id"), index=True)
    order_date = db.Column(db.Date)
    promised_date = db.Column(db.Date)
    eta_date = db.Column(db.Date)
    actual_date = db.Column(db.Date)
    currency = db.Column(db.String(3), default="INR")
    expedited = db.Column(db.Boolean, default=False)
    mode = db.Column(db.String(12), default="ROAD")
    lane = db.Column(db.String(120))
    delay_reason = db.Column(db.String(120))
    supplier = db.relationship("Supplier")
    dest = db.relationship("Location")
    lines = db.relationship("PurchaseOrderLine", backref="po", cascade="all, delete-orphan")


class PurchaseOrderLine(CanonicalMixin, db.Model):
    __tablename__ = "purchase_order_line"
    id = db.Column(db.Integer, primary_key=True)
    po_id = db.Column(db.Integer, db.ForeignKey("purchase_order.id"), nullable=False, index=True)
    line_no = db.Column(db.Integer, default=1)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False, index=True)
    qty_ordered = db.Column(db.Float, nullable=False)
    qty_received = db.Column(db.Float, default=0)
    qty_in_transit = db.Column(db.Float, default=0)     # shipped, not yet received. on_order = ordered - received - in_transit
    uom = db.Column(db.String(12), default="EA")
    unit_price = db.Column(db.Float)
    promised_date = db.Column(db.Date)
    eta_date = db.Column(db.Date)
    item = db.relationship("Item")


class SalesOrder(CanonicalMixin, db.Model):
    __tablename__ = "sales_order"
    id = db.Column(db.Integer, primary_key=True)
    so_number = db.Column(db.String(40), unique=True, nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), index=True)
    ship_from_location_id = db.Column(db.Integer, db.ForeignKey("location.id"), index=True)
    order_date = db.Column(db.Date)
    requested_date = db.Column(db.Date)
    promised_date = db.Column(db.Date)
    channel = db.Column(db.String(20))
    priority = db.Column(db.Integer, default=3)
    customer = db.relationship("Customer")
    ship_from = db.relationship("Location")
    lines = db.relationship("SalesOrderLine", backref="so", cascade="all, delete-orphan")


class SalesOrderLine(CanonicalMixin, db.Model):
    __tablename__ = "sales_order_line"
    id = db.Column(db.Integer, primary_key=True)
    so_id = db.Column(db.Integer, db.ForeignKey("sales_order.id"), nullable=False, index=True)
    line_no = db.Column(db.Integer, default=1)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False, index=True)
    qty_ordered = db.Column(db.Float, nullable=False)
    qty_shipped = db.Column(db.Float, default=0)
    unit_price = db.Column(db.Float)
    requested_date = db.Column(db.Date)
    ship_date = db.Column(db.Date)
    delivered_date = db.Column(db.Date)
    item = db.relationship("Item")


class TransferOrder(CanonicalMixin, db.Model):
    __tablename__ = "transfer_order"
    id = db.Column(db.Integer, primary_key=True)
    to_number = db.Column(db.String(40), unique=True, nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False, index=True)
    from_location_id = db.Column(db.Integer, db.ForeignKey("location.id"), index=True)
    to_location_id = db.Column(db.Integer, db.ForeignKey("location.id"), index=True)
    qty = db.Column(db.Float, nullable=False)
    qty_received = db.Column(db.Float, default=0)
    ship_date = db.Column(db.Date)
    eta_date = db.Column(db.Date)
    promised_date = db.Column(db.Date)
    actual_date = db.Column(db.Date)
    mode = db.Column(db.String(12), default="ROAD")
    lot_id = db.Column(db.Integer, db.ForeignKey("lot.id"))
    item = db.relationship("Item")
    from_loc = db.relationship("Location", foreign_keys=[from_location_id])
    to_loc = db.relationship("Location", foreign_keys=[to_location_id])
    # status: PLANNED / OPEN (approved, not shipped) / IN_TRANSIT / RECEIVED / CANCELLED  (CanonicalMixin.status)


class ProductionOrder(CanonicalMixin, db.Model):
    __tablename__ = "production_order"
    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(40), unique=True, nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False, index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"), index=True)
    qty = db.Column(db.Float, nullable=False)
    qty_completed = db.Column(db.Float, default=0)
    start_date = db.Column(db.Date)
    due_date = db.Column(db.Date)
    priority = db.Column(db.Integer, default=3)
    bom_revision = db.Column(db.String(10))
    item = db.relationship("Item")
    location = db.relationship("Location")


class Shipment(CanonicalMixin, db.Model):
    __tablename__ = "shipment"
    id = db.Column(db.Integer, primary_key=True)
    shipment_no = db.Column(db.String(40), unique=True, nullable=False, index=True)
    ref_type = db.Column(db.String(4))                    # PO / TO
    ref_number = db.Column(db.String(40), index=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"))
    carrier_id = db.Column(db.Integer, db.ForeignKey("carrier.id"))
    origin_location_id = db.Column(db.Integer, db.ForeignKey("location.id"))
    dest_location_id = db.Column(db.Integer, db.ForeignKey("location.id"), index=True)
    origin_name = db.Column(db.String(80))
    mode = db.Column(db.String(12), default="ROAD")
    lane = db.Column(db.String(120), index=True)
    port = db.Column(db.String(60))
    dispatch_date = db.Column(db.Date)
    promised_date = db.Column(db.Date)
    eta_date = db.Column(db.Date)
    actual_date = db.Column(db.Date)
    delay_days = db.Column(db.Float, default=0)
    delay_reason = db.Column(db.String(120))
    weight_kg = db.Column(db.Float, default=0)
    distance_km = db.Column(db.Float, default=0)
    expedited = db.Column(db.Boolean, default=False)
    tracking = db.Column(db.String(60))
    supplier = db.relationship("Supplier")
    dest = db.relationship("Location", foreign_keys=[dest_location_id])
    carrier = db.relationship("Carrier")
    lines = db.relationship("ShipmentLine", backref="shipment", cascade="all, delete-orphan")
    # status: PLANNED / DISPATCHED / IN_TRANSIT / DELAYED / DELIVERED


class ShipmentLine(db.Model):
    __tablename__ = "shipment_line"
    id = db.Column(db.Integer, primary_key=True)
    shipment_id = db.Column(db.Integer, db.ForeignKey("shipment.id"), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False, index=True)
    qty = db.Column(db.Float, nullable=False)
    po_line_id = db.Column(db.Integer, db.ForeignKey("purchase_order_line.id"))
    item = db.relationship("Item")
