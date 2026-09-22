"""Inventory state: lots, serials, balances, ledger transactions, external balances, reusable assets, returns."""
from ..extensions import db
from .base import CanonicalMixin, utcnow


class Lot(CanonicalMixin, db.Model):
    __tablename__ = "lot"
    __table_args__ = (db.UniqueConstraint("item_id", "lot_no", name="uq_lot_item_no"),)
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False, index=True)
    lot_no = db.Column(db.String(60), nullable=False, index=True)
    kind = db.Column(db.String(8), default="LOT")               # LOT / BATCH
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"))
    manufacture_date = db.Column(db.Date)
    expiry_date = db.Column(db.Date)
    received_date = db.Column(db.Date)
    quality_status = db.Column(db.String(12), default="RELEASED")   # RELEASED / QUARANTINE / REJECTED / BLOCKED
    country = db.Column(db.String(60))
    item = db.relationship("Item")
    supplier = db.relationship("Supplier")


class SerialNumber(CanonicalMixin, db.Model):
    __tablename__ = "serial_number"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False, index=True)
    serial_no = db.Column(db.String(80), nullable=False, unique=True)
    lot_id = db.Column(db.Integer, db.ForeignKey("lot.id"))
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"))
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"))
    sold_date = db.Column(db.Date)


class InventoryBalance(CanonicalMixin, db.Model):
    """Physical stock partitioned into mutually-exclusive `state`s. Claims (allocations) and pipeline
    (in-transit / on-order) are held elsewhere, so no unit is ever counted twice."""
    __tablename__ = "inventory_balance"
    __table_args__ = (db.UniqueConstraint("item_id", "location_id", "lot_id", "state", name="uq_balance"),)
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False, index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"), nullable=False, index=True)
    lot_id = db.Column(db.Integer, db.ForeignKey("lot.id"), index=True)
    state = db.Column(db.String(20), default="UNRESTRICTED", nullable=False)
    quantity = db.Column(db.Float, default=0.0, nullable=False)
    uom = db.Column(db.String(12), default="EA")
    last_movement_at = db.Column(db.DateTime)
    last_receipt_at = db.Column(db.DateTime)
    item = db.relationship("Item")
    location = db.relationship("Location")
    lot = db.relationship("Lot")


class InventoryTransaction(CanonicalMixin, db.Model):
    """Append-only ledger. Corrections are new reversing transactions, never edits."""
    __tablename__ = "inventory_transaction"
    id = db.Column(db.Integer, primary_key=True)
    txn_id = db.Column(db.String(64), unique=True, nullable=False)     # idempotency key -> duplicate detection
    txn_type = db.Column(db.String(24), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False, index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"), nullable=False, index=True)
    to_location_id = db.Column(db.Integer, db.ForeignKey("location.id"))
    lot_id = db.Column(db.Integer, db.ForeignKey("lot.id"), index=True)
    quantity = db.Column(db.Float, nullable=False)                     # always positive magnitude (base UOM)
    uom = db.Column(db.String(12), default="EA")
    state_from = db.Column(db.String(20))
    state_to = db.Column(db.String(20))
    on_hand_before = db.Column(db.Float)                               # opening (item, location) on-hand
    on_hand_after = db.Column(db.Float)                                # closing
    occurred_at = db.Column(db.DateTime, default=utcnow, index=True)
    ref_type = db.Column(db.String(20))
    ref_id = db.Column(db.String(60), index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"))
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"))
    reason = db.Column(db.String(255))
    actor = db.Column(db.String(60), default="system")
    reversal_of_id = db.Column(db.Integer, db.ForeignKey("inventory_transaction.id"))
    item = db.relationship("Item")
    location = db.relationship("Location", foreign_keys=[location_id])


class ExternalBalance(CanonicalMixin, db.Model):
    """Balance reported by an external system (ERP / WMS / 3PL / PHYSICAL) for reconciliation."""
    __tablename__ = "external_balance"
    id = db.Column(db.Integer, primary_key=True)
    system = db.Column(db.String(20), nullable=False, index=True)      # ERP / WMS / 3PL / PHYSICAL
    sku_raw = db.Column(db.String(60), nullable=False)
    location_raw = db.Column(db.String(40), nullable=False)
    lot_no = db.Column(db.String(60))
    quantity = db.Column(db.Float, nullable=False)
    uom = db.Column(db.String(12), default="EA")
    last_sync = db.Column(db.DateTime, default=utcnow)
    snapshot_at = db.Column(db.DateTime)


class ReusableAsset(CanonicalMixin, db.Model):
    __tablename__ = "reusable_asset"
    id = db.Column(db.Integer, primary_key=True)
    pool_code = db.Column(db.String(40), nullable=False, index=True)
    asset_type = db.Column(db.String(24))                              # PALLET / TOTE / DUNNAGE / CONTAINER / PACKAGING
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"))
    owner = db.Column(db.String(60))
    qty_available = db.Column(db.Float, default=0)
    qty_in_transit = db.Column(db.Float, default=0)
    qty_repair = db.Column(db.Float, default=0)
    qty_lost = db.Column(db.Float, default=0)
    unit_value = db.Column(db.Float, default=0)
    condition = db.Column(db.String(12), default="GOOD")
    location = db.relationship("Location")


class ReturnRecord(CanonicalMixin, db.Model):
    __tablename__ = "return_record"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"))
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"))
    qty = db.Column(db.Float, default=0)
    return_date = db.Column(db.Date)
    reason = db.Column(db.String(60))
    disposition = db.Column(db.String(20))       # RESTOCK / REPAIR / REFURBISH / RECYCLE / SCRAP / PENDING
    item = db.relationship("Item")
    location = db.relationship("Location")


class KpiSnapshot(db.Model):
    """Time series of headline metrics for trend charts. `source` says whether it was measured or demo-generated."""
    __tablename__ = "kpi_snapshot"
    id = db.Column(db.Integer, primary_key=True)
    as_of = db.Column(db.Date, index=True)
    metric = db.Column(db.String(40), index=True)
    value = db.Column(db.Float)
    scope = db.Column(db.String(60), default="ALL")
    source = db.Column(db.String(12), default="MEASURED")
