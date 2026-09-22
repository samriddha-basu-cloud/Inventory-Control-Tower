"""Demand, forecast, lead-time observations, policies, allocation and pegging."""
from ..extensions import db
from .base import CanonicalMixin, utcnow


class Demand(CanonicalMixin, db.Model):
    """Historical demand actuals (consumption / shipments). `granularity` D or W."""
    __tablename__ = "demand"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False, index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"), nullable=False, index=True)
    period_start = db.Column(db.Date, nullable=False, index=True)
    granularity = db.Column(db.String(1), default="W")
    qty = db.Column(db.Float, nullable=False)
    channel = db.Column(db.String(20))
    censored = db.Column(db.Boolean, default=False)      # demand was stock-out constrained (FIT should treat as lower bound)


class Forecast(CanonicalMixin, db.Model):
    __tablename__ = "forecast"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False, index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"), nullable=False, index=True)
    period_start = db.Column(db.Date, nullable=False, index=True)
    granularity = db.Column(db.String(1), default="W")
    qty = db.Column(db.Float, nullable=False)
    forecast_type = db.Column(db.String(12), default="BASELINE")   # BASELINE / CONSENSUS / ADJUSTED
    source = db.Column(db.String(12), default="FIT")               # FIT / ERP / UPLOAD / MANUAL
    p10 = db.Column(db.Float)
    p90 = db.Column(db.Float)
    version = db.Column(db.String(40))
    model_name = db.Column(db.String(60))
    issued_at = db.Column(db.Date)                                  # when the forecast was published (for bias vs actual)


class LeadTimeObservation(CanonicalMixin, db.Model):
    __tablename__ = "lead_time_observation"
    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"), index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), index=True)
    dest_location_id = db.Column(db.Integer, db.ForeignKey("location.id"), index=True)
    origin = db.Column(db.String(80))
    lane = db.Column(db.String(120), index=True)
    mode = db.Column(db.String(12))
    carrier_id = db.Column(db.Integer, db.ForeignKey("carrier.id"))
    po_ref = db.Column(db.String(40))
    ordered_date = db.Column(db.Date)
    received_date = db.Column(db.Date)
    lead_time_days = db.Column(db.Float, nullable=False)
    promised_days = db.Column(db.Float)
    qty_ordered = db.Column(db.Float)
    qty_received = db.Column(db.Float)
    rejected_qty = db.Column(db.Float, default=0)
    expedited = db.Column(db.Boolean, default=False)


class _PolicyBase(CanonicalMixin):
    """Scope hierarchy: GLOBAL < INDUSTRY < REGION < NODE < CATEGORY < SKU < SKU_LOCATION (see rules/policies.py)."""
    id = db.Column(db.Integer, primary_key=True)
    scope_level = db.Column(db.String(14), default="GLOBAL", nullable=False, index=True)
    scope_key = db.Column(db.String(120), default="*", nullable=False)   # e.g. 'AUTOMOTIVE', 'NORTH', 'DC-01', 'SKU-1|DC-01'
    params = db.Column(db.JSON, default=dict)
    notes = db.Column(db.String(255))
    active = db.Column(db.Boolean, default=True)


class SafetyStockPolicy(_PolicyBase, db.Model):
    """params: method, service_level, fixed_qty|days_cover overrides, review_days, lt_basis"""
    __tablename__ = "safety_stock_policy"


class ReplenishmentPolicy(_PolicyBase, db.Model):
    """params: policy (MIN_MAX/ROP/...), min, max, fixed_qty, review_days, order_up_to, cards, container_qty, jit_window_days..."""
    __tablename__ = "replenishment_policy"


class ControlPolicy(_PolicyBase, db.Model):
    """Generic hierarchical policy for: service_level, moq, order_multiple, allocation, priority, expiry,
    transfer, approval, excess."""
    __tablename__ = "control_policy"
    policy_type = db.Column(db.String(24), nullable=False, index=True)


class Allocation(CanonicalMixin, db.Model):
    """A claim against physical stock. Available = allocatable stock - active claims."""
    __tablename__ = "allocation"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False, index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"), nullable=False, index=True)
    lot_id = db.Column(db.Integer, db.ForeignKey("lot.id"))
    demand_type = db.Column(db.String(8), default="SO")     # SO / PROD / TO / MANUAL
    demand_ref = db.Column(db.String(40), index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"))
    qty = db.Column(db.Float, nullable=False)
    alloc_type = db.Column(db.String(10), default="ALLOCATED")  # ALLOCATED / COMMITTED / RESERVED
    policy = db.Column(db.String(60))
    created_at = db.Column(db.DateTime, default=utcnow)
    item = db.relationship("Item")
    location = db.relationship("Location")


class Peg(CanonicalMixin, db.Model):
    """Supply <-> demand link (basic pegging). One-to-one, one-to-many and many-to-one all representable."""
    __tablename__ = "peg"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False, index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"), nullable=False, index=True)
    supply_type = db.Column(db.String(10))    # ONHAND / PO / TO / PROD
    supply_ref = db.Column(db.String(60))
    demand_type = db.Column(db.String(8))
    demand_ref = db.Column(db.String(40), index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"))
    qty = db.Column(db.Float, nullable=False)
    expected_date = db.Column(db.Date)
    demand_date = db.Column(db.Date)
    peg_status = db.Column(db.String(12), default="PLANNED")    # PLANNED / COMMITTED
    created_at = db.Column(db.DateTime, default=utcnow)
    item = db.relationship("Item")
    location = db.relationship("Location")
