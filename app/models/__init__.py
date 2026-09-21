"""
Canonical data model for Inventory Control Tower.

Design notes
------------
* One physical database serves a single organization in this MVP (the
  Organization/User rows exist so role-based UI emphasis and audit
  attribution work, but there is no login/auth gate yet - see docs/governance.md).
* Inventory truth lives in `InventoryLedger`: one row per (item, location, status)
  bucket, e.g. ON_HAND, ALLOCATED, IN_TRANSIT, QUARANTINE. Nothing here treats
  "on hand" as "available" - availability is derived in app/services.
* All monetary fields are stored in the item's transaction currency; conversion
  to a reporting currency happens in app/utils/currency.py using ExchangeRate.
"""
from datetime import datetime
from app.extensions import db


# ---------------------------------------------------------------------------
# Organization / Users / Governance
# ---------------------------------------------------------------------------

class Organization(db.Model):
    __tablename__ = "organizations"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, default="Demo Organization")
    base_currency = db.Column(db.String(8), default="INR")
    industry_profile = db.Column(db.String(40), default="fmcg")  # see app/utils/industry_profiles.py
    inventory_position_formula = db.Column(
        db.String(255), default="on_hand + on_order + in_transit - allocated - backorders"
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(180), unique=True, nullable=False)
    role = db.Column(db.String(40), default="planner")
    # executive | planner | demand_planner | supply_planner | procurement |
    # warehouse | finance | operations | administrator
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AuditLog(db.Model):
    __tablename__ = "audit_log"
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    actor = db.Column(db.String(120), default="system")
    action = db.Column(db.String(80), nullable=False)
    entity_type = db.Column(db.String(60))
    entity_id = db.Column(db.String(60))
    details = db.Column(db.Text)
    data_version = db.Column(db.String(40))


# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------

class Supplier(db.Model):
    __tablename__ = "suppliers"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False)
    name = db.Column(db.String(160), nullable=False)
    country = db.Column(db.String(80))
    tier = db.Column(db.Integer, default=1)
    otif_pct = db.Column(db.Float)              # trailing OTIF, computed by risk_service but cached here
    defect_rate_pct = db.Column(db.Float, default=0.0)
    single_source_risk = db.Column(db.Boolean, default=False)
    lead_time_mean_days = db.Column(db.Float, default=14)
    lead_time_std_days = db.Column(db.Float, default=3)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    items = db.relationship("Item", back_populates="primary_supplier")


class Customer(db.Model):
    __tablename__ = "customers"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False)
    name = db.Column(db.String(160), nullable=False)
    segment = db.Column(db.String(40), default="standard")   # strategic | standard | opportunistic
    priority_weight = db.Column(db.Float, default=1.0)
    region = db.Column(db.String(80))
    service_level_target_pct = db.Column(db.Float, default=95.0)


class Location(db.Model):
    __tablename__ = "locations"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False)
    name = db.Column(db.String(160), nullable=False)
    node_type = db.Column(db.String(30), default="dc")  # supplier|plant|dc|store|customer
    region = db.Column(db.String(80))
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    storage_capacity_units = db.Column(db.Float)
    review_period_days = db.Column(db.Integer, default=7)
    parent_location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=True)

    parent = db.relationship("Location", remote_side=[id])


class Item(db.Model):
    __tablename__ = "items"
    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(60), unique=True, nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(80))
    product_family = db.Column(db.String(80))
    base_uom = db.Column(db.String(16), default="EA")
    unit_cost = db.Column(db.Float, default=0.0)
    unit_price = db.Column(db.Float, default=0.0)
    currency = db.Column(db.String(8), default="INR")

    moq = db.Column(db.Float, default=0.0)
    order_multiple = db.Column(db.Float, default=1.0)
    shelf_life_days = db.Column(db.Integer, nullable=True)
    is_batch_tracked = db.Column(db.Boolean, default=False)
    is_serial_tracked = db.Column(db.Boolean, default=False)

    primary_supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=True)
    primary_supplier = db.relationship("Supplier", back_populates="items")

    # Policy-relevant, editable overrides (planner overrides tracked via PolicyOverride)
    service_level_target_pct = db.Column(db.Float, default=95.0)
    ved_class = db.Column(db.String(1), default="D")  # V/E/D criticality
    is_active = db.Column(db.Boolean, default=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class UomConversion(db.Model):
    __tablename__ = "uom_conversions"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    from_uom = db.Column(db.String(16), nullable=False)
    to_uom = db.Column(db.String(16), nullable=False)
    factor = db.Column(db.Float, nullable=False)  # 1 from_uom = factor * to_uom


class ExchangeRate(db.Model):
    __tablename__ = "exchange_rates"
    id = db.Column(db.Integer, primary_key=True)
    from_currency = db.Column(db.String(8), nullable=False)
    to_currency = db.Column(db.String(8), nullable=False)
    rate = db.Column(db.Float, nullable=False)
    as_of = db.Column(db.Date, default=datetime.utcnow)


class BomComponent(db.Model):
    __tablename__ = "bom_components"
    id = db.Column(db.Integer, primary_key=True)
    parent_item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    component_item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    quantity_per = db.Column(db.Float, nullable=False, default=1.0)
    is_alternate_group = db.Column(db.String(20), nullable=True)  # components sharing a group are substitutes

    parent_item = db.relationship("Item", foreign_keys=[parent_item_id])
    component_item = db.relationship("Item", foreign_keys=[component_item_id])


# ---------------------------------------------------------------------------
# Inventory ledger (the canonical truth)
# ---------------------------------------------------------------------------

INVENTORY_STATUSES = [
    "ON_HAND", "AVAILABLE", "ALLOCATED", "COMMITTED", "QUARANTINED", "BLOCKED",
    "DAMAGED", "IN_TRANSIT", "WIP", "ON_ORDER", "RETURN_IN_TRANSIT", "RETURNED",
    "REPAIR", "SCRAP", "EXPIRED", "EXCESS", "OBSOLETE",
]


class InventoryLedger(db.Model):
    """One row per (item, location, status, batch) snapshot balance."""
    __tablename__ = "inventory_ledger"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False, index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=False, index=True)
    status = db.Column(db.String(24), nullable=False, default="ON_HAND", index=True)
    quantity = db.Column(db.Float, nullable=False, default=0.0)
    batch_code = db.Column(db.String(60), nullable=True)
    manufacture_date = db.Column(db.Date, nullable=True)
    expiry_date = db.Column(db.Date, nullable=True)
    as_of = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    item = db.relationship("Item")
    location = db.relationship("Location")


class InventoryTransaction(db.Model):
    """Append-only movement log feeding the ledger and audit/timeline views."""
    __tablename__ = "inventory_transactions"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False, index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=False, index=True)
    txn_type = db.Column(db.String(30), nullable=False)  # RECEIPT|SHIPMENT|TRANSFER|ADJUSTMENT|RETURN|SCRAP...
    quantity = db.Column(db.Float, nullable=False)
    from_status = db.Column(db.String(24), nullable=True)
    to_status = db.Column(db.String(24), nullable=True)
    reference_type = db.Column(db.String(30), nullable=True)  # PO|TO|SO|ADJ
    reference_id = db.Column(db.Integer, nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    item = db.relationship("Item")
    location = db.relationship("Location")


# ---------------------------------------------------------------------------
# Demand / forecast history
# ---------------------------------------------------------------------------

class DemandHistory(db.Model):
    __tablename__ = "demand_history"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False, index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=False, index=True)
    period_date = db.Column(db.Date, nullable=False, index=True)
    quantity = db.Column(db.Float, nullable=False, default=0.0)
    revenue = db.Column(db.Float, default=0.0)

    item = db.relationship("Item")
    location = db.relationship("Location")


class ForecastPoint(db.Model):
    __tablename__ = "forecast_points"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False, index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=False, index=True)
    period_date = db.Column(db.Date, nullable=False, index=True)
    forecast_qty = db.Column(db.Float, nullable=False)
    p10_qty = db.Column(db.Float, nullable=True)
    p90_qty = db.Column(db.Float, nullable=True)
    actual_qty = db.Column(db.Float, nullable=True)  # filled in after the period closes -> forecast error


# ---------------------------------------------------------------------------
# Orders / shipments
# ---------------------------------------------------------------------------

class PurchaseOrder(db.Model):
    __tablename__ = "purchase_orders"
    id = db.Column(db.Integer, primary_key=True)
    po_number = db.Column(db.String(40), unique=True, nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=False)
    destination_location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=False)
    order_date = db.Column(db.Date, default=datetime.utcnow)
    original_eta = db.Column(db.Date, nullable=True)
    current_eta = db.Column(db.Date, nullable=True)
    status = db.Column(db.String(20), default="OPEN")  # OPEN|IN_TRANSIT|RECEIVED|DELAYED|CANCELLED
    is_expedited = db.Column(db.Boolean, default=False)

    supplier = db.relationship("Supplier")
    destination = db.relationship("Location")
    lines = db.relationship("PurchaseOrderLine", back_populates="po", cascade="all, delete-orphan")


class PurchaseOrderLine(db.Model):
    __tablename__ = "purchase_order_lines"
    id = db.Column(db.Integer, primary_key=True)
    po_id = db.Column(db.Integer, db.ForeignKey("purchase_orders.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    quantity_ordered = db.Column(db.Float, nullable=False)
    quantity_received = db.Column(db.Float, default=0.0)
    unit_cost = db.Column(db.Float, default=0.0)

    po = db.relationship("PurchaseOrder", back_populates="lines")
    item = db.relationship("Item")


class TransferOrder(db.Model):
    __tablename__ = "transfer_orders"
    id = db.Column(db.Integer, primary_key=True)
    to_number = db.Column(db.String(40), unique=True, nullable=False)
    source_location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=False)
    destination_location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    quantity = db.Column(db.Float, nullable=False)
    transport_mode = db.Column(db.String(20), default="road")
    transit_days = db.Column(db.Float, default=2.0)
    transport_cost = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(20), default="RECOMMENDED")  # RECOMMENDED|APPROVED|IN_TRANSIT|COMPLETED|REJECTED
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    reason = db.Column(db.Text, nullable=True)

    source = db.relationship("Location", foreign_keys=[source_location_id])
    destination = db.relationship("Location", foreign_keys=[destination_location_id])
    item = db.relationship("Item")


class SalesOrder(db.Model):
    __tablename__ = "sales_orders"
    id = db.Column(db.Integer, primary_key=True)
    so_number = db.Column(db.String(40), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=False)
    order_date = db.Column(db.Date, default=datetime.utcnow)
    requested_date = db.Column(db.Date, nullable=True)
    status = db.Column(db.String(20), default="OPEN")

    customer = db.relationship("Customer")
    location = db.relationship("Location")
    lines = db.relationship("SalesOrderLine", back_populates="so", cascade="all, delete-orphan")


class SalesOrderLine(db.Model):
    __tablename__ = "sales_order_lines"
    id = db.Column(db.Integer, primary_key=True)
    so_id = db.Column(db.Integer, db.ForeignKey("sales_orders.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    quantity_ordered = db.Column(db.Float, nullable=False)
    quantity_allocated = db.Column(db.Float, default=0.0)
    quantity_shipped = db.Column(db.Float, default=0.0)
    unit_price = db.Column(db.Float, default=0.0)

    so = db.relationship("SalesOrder", back_populates="lines")
    item = db.relationship("Item")


class Shipment(db.Model):
    __tablename__ = "shipments"
    id = db.Column(db.Integer, primary_key=True)
    shipment_number = db.Column(db.String(40), unique=True, nullable=False)
    origin_location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=True)
    destination_location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=True)
    carrier = db.Column(db.String(80), nullable=True)
    mode = db.Column(db.String(20), default="road")
    ship_date = db.Column(db.Date, nullable=True)
    eta = db.Column(db.Date, nullable=True)
    status = db.Column(db.String(20), default="PLANNED")


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------

class SafetyStockPolicy(db.Model):
    __tablename__ = "safety_stock_policies"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=False)
    method = db.Column(db.String(40), default="demand_leadtime_variability")
    service_level_pct = db.Column(db.Float, default=95.0)
    review_period_days = db.Column(db.Integer, default=7)
    calculated_safety_stock = db.Column(db.Float, default=0.0)
    override_safety_stock = db.Column(db.Float, nullable=True)
    override_reason = db.Column(db.Text, nullable=True)
    override_by = db.Column(db.String(120), nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    item = db.relationship("Item")
    location = db.relationship("Location")

    @property
    def effective_safety_stock(self):
        return self.override_safety_stock if self.override_safety_stock is not None else self.calculated_safety_stock


class ReplenishmentPolicy(db.Model):
    __tablename__ = "replenishment_policies"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=False)
    policy_type = db.Column(db.String(20), default="rop_eoq")  # min_max|rop_eoq|periodic_review|kanban
    min_qty = db.Column(db.Float, nullable=True)
    max_qty = db.Column(db.Float, nullable=True)
    reorder_point = db.Column(db.Float, nullable=True)
    order_quantity = db.Column(db.Float, nullable=True)

    item = db.relationship("Item")
    location = db.relationship("Location")


# ---------------------------------------------------------------------------
# Optimization / scenarios (digital twin, never mutates live data)
# ---------------------------------------------------------------------------

class OptimizationRun(db.Model):
    __tablename__ = "optimization_runs"
    id = db.Column(db.Integer, primary_key=True)
    run_type = db.Column(db.String(40), nullable=False)  # safety_stock|meio|allocation|replenishment
    objective = db.Column(db.String(255))
    weights_json = db.Column(db.Text)
    constraints_json = db.Column(db.Text)
    status = db.Column(db.String(20), default="COMPLETED")  # COMPLETED|INFEASIBLE|FAILED
    input_snapshot_json = db.Column(db.Text)
    result_summary_json = db.Column(db.Text)
    created_by = db.Column(db.String(120), default="system")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    runtime_ms = db.Column(db.Integer, default=0)


class Scenario(db.Model):
    __tablename__ = "scenarios"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    base_version = db.Column(db.String(40), default="live")
    assumptions_json = db.Column(db.Text)  # e.g. {"demand_change_pct": 10, "lead_time_delta_days": 5}
    results_json = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default="DRAFT")  # DRAFT|RUNNING|COMPLETED|FAILED
    created_by = db.Column(db.String(120), default="system")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# Alerts / incidents / recommendations / approvals
# ---------------------------------------------------------------------------

class Alert(db.Model):
    __tablename__ = "alerts"
    id = db.Column(db.Integer, primary_key=True)
    alert_type = db.Column(db.String(40), nullable=False)
    # stockout_risk|excess|obsolescence|safety_stock_breach|rop_breach|late_po|
    # supplier_delay|expiry|service_level_breach|atp_shortage|capacity_constraint
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=True)
    severity = db.Column(db.String(4), default="P3")  # P1..P4
    financial_impact = db.Column(db.Float, default=0.0)
    service_impact_pct = db.Column(db.Float, default=0.0)
    message = db.Column(db.Text, nullable=False)
    incident_id = db.Column(db.Integer, db.ForeignKey("incidents.id"), nullable=True)
    status = db.Column(db.String(20), default="NEW")  # NEW|INVESTIGATING|ACTION_REQUIRED|APPROVED|EXECUTING|RESOLVED|CLOSED
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    resolved_at = db.Column(db.DateTime, nullable=True)

    item = db.relationship("Item")
    location = db.relationship("Location")


class Incident(db.Model):
    __tablename__ = "incidents"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    root_cause_observed = db.Column(db.Text, nullable=True)
    root_cause_likely = db.Column(db.Text, nullable=True)
    affected_sku_count = db.Column(db.Integer, default=0)
    affected_location_count = db.Column(db.Integer, default=0)
    financial_impact = db.Column(db.Float, default=0.0)
    owner = db.Column(db.String(120), nullable=True)
    status = db.Column(db.String(20), default="OPEN")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    resolved_at = db.Column(db.DateTime, nullable=True)

    alerts = db.relationship("Alert", backref="incident_ref", foreign_keys=[Alert.incident_id])


class Recommendation(db.Model):
    __tablename__ = "recommendations"
    id = db.Column(db.Integer, primary_key=True)
    rec_type = db.Column(db.String(40), nullable=False)  # transfer|expedite|safety_stock_change|reallocation
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=True)
    source_location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=True)
    destination_location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=True)
    quantity = db.Column(db.Float, nullable=True)
    reason_json = db.Column(db.Text)          # structured explainability payload
    expected_impact_json = db.Column(db.Text)
    confidence = db.Column(db.String(10), default="MEDIUM")  # HIGH|MEDIUM|LOW
    cost_estimate = db.Column(db.Float, default=0.0)
    autonomy_level = db.Column(db.Integer, default=1)  # 0 observe,1 recommend,2 approval,3 auto-guardrail
    status = db.Column(db.String(20), default="PENDING")  # PENDING|APPROVED|REJECTED|MODIFIED|EXECUTED
    alert_id = db.Column(db.Integer, db.ForeignKey("alerts.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    item = db.relationship("Item")
    source_location = db.relationship("Location", foreign_keys=[source_location_id])
    destination_location = db.relationship("Location", foreign_keys=[destination_location_id])


class Approval(db.Model):
    __tablename__ = "approvals"
    id = db.Column(db.Integer, primary_key=True)
    recommendation_id = db.Column(db.Integer, db.ForeignKey("recommendations.id"), nullable=False)
    decision = db.Column(db.String(20), nullable=False)  # APPROVE|REJECT|MODIFY|ESCALATE
    reason = db.Column(db.Text, nullable=True)
    decided_by = db.Column(db.String(120), default="planner@demo.org")
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    recommendation = db.relationship("Recommendation", backref="approvals")


class Execution(db.Model):
    __tablename__ = "executions"
    id = db.Column(db.Integer, primary_key=True)
    recommendation_id = db.Column(db.Integer, db.ForeignKey("recommendations.id"), nullable=False)
    mode = db.Column(db.String(20), default="SIMULATED")  # SIMULATED|LIVE
    result = db.Column(db.String(20), default="SUCCESS")
    detail = db.Column(db.Text, nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    recommendation = db.relationship("Recommendation", backref="executions")
