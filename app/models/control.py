"""Control layer: users/roles, settings, rules, KPIs, alerts, incidents, risks, actions, approvals, audit, scenarios."""
from ..extensions import db
from .base import CanonicalMixin, utcnow


class Role(db.Model):
    __tablename__ = "role"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), unique=True, nullable=False)
    description = db.Column(db.String(255))
    permissions = db.Column(db.JSON, default=list)


class User(db.Model):
    __tablename__ = "user_account"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(60), unique=True, nullable=False)
    name = db.Column(db.String(120))
    email = db.Column(db.String(160))
    role = db.Column(db.String(60), default="Inventory Planner")
    password_hash = db.Column(db.String(255))
    active = db.Column(db.Boolean, default=True)


class Setting(db.Model):
    """Key/value configuration (JSON). Keys are namespaced, e.g. 'thresholds.stockout', 'engine.holding_rate'."""
    __tablename__ = "setting"
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(120), unique=True, nullable=False)
    value = db.Column(db.JSON)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)
    updated_by = db.Column(db.String(60))


class IndustryProfile(db.Model):
    __tablename__ = "industry_profile"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(30), unique=True, nullable=False)
    name = db.Column(db.String(80))
    description = db.Column(db.String(400))
    terminology = db.Column(db.JSON, default=dict)
    kpis = db.Column(db.JSON, default=list)
    rules = db.Column(db.JSON, default=list)          # rule codes enabled by the profile
    dashboards = db.Column(db.JSON, default=list)
    alerts = db.Column(db.JSON, default=list)
    policies = db.Column(db.JSON, default=dict)       # setting overrides applied while active
    recommended_actions = db.Column(db.JSON, default=list)
    focus = db.Column(db.JSON, default=list)


class InventoryStateDef(db.Model):
    """Configurable inventory status catalogue (the 'exact statuses must be configurable' requirement)."""
    __tablename__ = "inventory_state_def"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    label = db.Column(db.String(60))
    category = db.Column(db.String(12))               # PHYSICAL / CLAIM / PIPELINE / DERIVED
    counts_on_hand = db.Column(db.Boolean, default=False)
    allocatable = db.Column(db.Boolean, default=False)
    counts_in_position = db.Column(db.Boolean, default=False)
    description = db.Column(db.String(255))
    enabled = db.Column(db.Boolean, default=True)


class Rule(db.Model):
    """Data-driven alert/detection rule: `condition` is evaluated against a metric context by rules/rule_engine.py."""
    __tablename__ = "rule"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False)
    name = db.Column(db.String(120))
    description = db.Column(db.String(400))
    alert_type = db.Column(db.String(30), nullable=False)
    scope = db.Column(db.String(12), default="pair")   # pair / supply / supplier / lot / recon / sync
    condition = db.Column(db.JSON, default=dict)
    severity = db.Column(db.JSON, default=dict)        # {"default":"MEDIUM","when":[{"metric":..,"op":..,"value":..,"severity":..}]}
    industries = db.Column(db.JSON, default=list)      # [] = all
    recommended_action = db.Column(db.String(60))
    suppress_hours = db.Column(db.Integer, default=0)
    escalate_after_hours = db.Column(db.Integer, default=48)
    enabled = db.Column(db.Boolean, default=True)
    version = db.Column(db.Integer, default=1)


class KpiDefinition(db.Model):
    __tablename__ = "kpi_definition"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False)
    name = db.Column(db.String(80))
    category = db.Column(db.String(20))               # Availability / Efficiency / Risk / Financial / Service / Quality / Sustainability
    formula = db.Column(db.String(400))               # safe expression over engine measures
    unit = db.Column(db.String(12))                   # ratio / pct / days / money / count
    direction = db.Column(db.String(12), default="higher")   # higher = better | lower = better
    window_days = db.Column(db.Integer, default=90)
    entity_level = db.Column(db.String(12), default="enterprise")
    watch = db.Column(db.Float)
    attention = db.Column(db.Float)
    critical = db.Column(db.Float)
    alert_threshold = db.Column(db.Float)
    enabled = db.Column(db.Boolean, default=True)
    description = db.Column(db.String(400))


class Incident(db.Model):
    __tablename__ = "incident"
    id = db.Column(db.Integer, primary_key=True)
    incident_no = db.Column(db.String(20), unique=True)
    title = db.Column(db.String(255))
    root_cause = db.Column(db.String(255))
    cause_type = db.Column(db.String(30))
    status = db.Column(db.String(20), default="New")
    severity = db.Column(db.String(10))
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)
    cluster_key = db.Column(db.String(160), index=True)
    dims = db.Column(db.JSON, default=dict)            # shared dimensions that formed the cluster
    impact = db.Column(db.JSON, default=dict)          # aggregated impacts
    priority_score = db.Column(db.Float, default=0)
    owner = db.Column(db.String(60))
    explain = db.Column(db.JSON, default=dict)         # 8-question explainability block
    alerts = db.relationship("Alert", backref="incident")


class Alert(db.Model):
    __tablename__ = "alert"
    id = db.Column(db.Integer, primary_key=True)
    alert_no = db.Column(db.String(20), unique=True)
    dedupe_key = db.Column(db.String(200), index=True)
    rule_code = db.Column(db.String(40), index=True)
    alert_type = db.Column(db.String(30), index=True)
    severity = db.Column(db.String(10), index=True)
    status = db.Column(db.String(20), default="New", index=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    first_seen = db.Column(db.DateTime, default=utcnow)
    last_seen = db.Column(db.DateTime, default=utcnow)
    occurrences = db.Column(db.Integer, default=1)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"), index=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"), index=True)
    entity_ref = db.Column(db.String(60))              # PO/shipment/lot etc.
    title = db.Column(db.String(255))
    message = db.Column(db.Text)
    root_cause = db.Column(db.String(255))
    dims = db.Column(db.JSON, default=dict)            # shipment/po/supplier/lane/node/sku/prod_order
    metrics = db.Column(db.JSON, default=dict)         # evidence values that triggered the rule
    impact = db.Column(db.JSON, default=dict)          # value_at_risk, revenue_at_risk, service, production, customers
    recommended_action = db.Column(db.String(120))
    owner = db.Column(db.String(60))
    priority_score = db.Column(db.Float, default=0)
    priority_breakdown = db.Column(db.JSON, default=dict)
    time_to_impact_days = db.Column(db.Float)
    suppressed = db.Column(db.Boolean, default=False)
    suppressed_reason = db.Column(db.String(160))
    escalated = db.Column(db.Boolean, default=False)
    incident_id = db.Column(db.Integer, db.ForeignKey("incident.id"), index=True)
    item = db.relationship("Item")
    location = db.relationship("Location")
    supplier = db.relationship("Supplier")


class Risk(db.Model):
    """Unified risk register entry."""
    __tablename__ = "risk"
    id = db.Column(db.Integer, primary_key=True)
    risk_id = db.Column(db.String(24), unique=True)
    risk_type = db.Column(db.String(30), index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"), index=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"), index=True)
    cause = db.Column(db.String(255))
    probability = db.Column(db.Float)
    impact_value = db.Column(db.Float)
    exposure = db.Column(db.Float)                     # probability x impact
    severity = db.Column(db.String(10))
    recommended_action = db.Column(db.String(160))
    owner = db.Column(db.String(60))
    status = db.Column(db.String(16), default="OPEN")
    created_at = db.Column(db.DateTime, default=utcnow)
    item = db.relationship("Item")
    location = db.relationship("Location")
    supplier = db.relationship("Supplier")


class Recommendation(db.Model):
    __tablename__ = "recommendation"
    id = db.Column(db.Integer, primary_key=True)
    rec_no = db.Column(db.String(20), unique=True)
    kind = db.Column(db.String(30), index=True)        # REPLENISH / EXPEDITE / TRANSFER / ALLOCATE / REVIEW_EXPIRY / ...
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"), index=True)
    title = db.Column(db.String(255))
    summary = db.Column(db.Text)
    payload = db.Column(db.JSON, default=dict)         # full explainer: inputs, formula, options
    confidence = db.Column(db.Float)
    data_quality = db.Column(db.Float)
    priority_score = db.Column(db.Float, default=0)
    cost = db.Column(db.Float, default=0)
    status = db.Column(db.String(12), default="OPEN")  # OPEN / ACTIONED / DISMISSED
    created_at = db.Column(db.DateTime, default=utcnow)
    alert_id = db.Column(db.Integer, db.ForeignKey("alert.id"))
    item = db.relationship("Item")
    location = db.relationship("Location")


class Action(db.Model):
    __tablename__ = "action"
    id = db.Column(db.Integer, primary_key=True)
    action_no = db.Column(db.String(20), unique=True)
    action_type = db.Column(db.String(30), index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"))
    to_location_id = db.Column(db.Integer, db.ForeignKey("location.id"))
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"))
    qty = db.Column(db.Float)
    need_date = db.Column(db.Date)
    cost = db.Column(db.Float, default=0)
    why = db.Column(db.Text)
    impact = db.Column(db.JSON, default=dict)
    risk_level = db.Column(db.String(10))
    confidence = db.Column(db.Float)
    mode = db.Column(db.String(20), default="RECOMMENDATION")   # RECOMMENDATION / SIMULATION_ONLY / LIVE
    status = db.Column(db.String(24), default="PROPOSED", index=True)
    approval_required = db.Column(db.Boolean, default=True)
    approval_reason = db.Column(db.String(255))
    required_role = db.Column(db.String(60))
    autonomy = db.Column(db.JSON, default=dict)
    policy_check = db.Column(db.JSON, default=dict)
    simulation = db.Column(db.JSON, default=dict)
    alternatives = db.Column(db.JSON, default=list)
    payload = db.Column(db.JSON, default=dict)
    alert_id = db.Column(db.Integer, db.ForeignKey("alert.id"))
    recommendation_id = db.Column(db.Integer, db.ForeignKey("recommendation.id"))
    created_by = db.Column(db.String(60))
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)
    item = db.relationship("Item")
    location = db.relationship("Location", foreign_keys=[location_id])
    to_location = db.relationship("Location", foreign_keys=[to_location_id])
    supplier = db.relationship("Supplier")
    approvals = db.relationship("Approval", backref="action", order_by="Approval.id")
    executions = db.relationship("Execution", backref="action", order_by="Execution.id")


class Approval(db.Model):
    __tablename__ = "approval"
    id = db.Column(db.Integer, primary_key=True)
    action_id = db.Column(db.Integer, db.ForeignKey("action.id"), nullable=False, index=True)
    approver = db.Column(db.String(60))
    role = db.Column(db.String(60))
    decision = db.Column(db.String(12))                # APPROVE / REJECT / MODIFY / ESCALATE
    comment = db.Column(db.Text)
    modified_payload = db.Column(db.JSON)
    decided_at = db.Column(db.DateTime, default=utcnow)


class Execution(db.Model):
    __tablename__ = "execution"
    id = db.Column(db.Integer, primary_key=True)
    action_id = db.Column(db.Integer, db.ForeignKey("action.id"), nullable=False, index=True)
    connector = db.Column(db.String(40))
    is_mock = db.Column(db.Boolean, default=True)
    mode = db.Column(db.String(20))
    request = db.Column(db.JSON)
    response = db.Column(db.JSON)
    status = db.Column(db.String(16))                  # SIMULATED / EXECUTED / FAILED
    verified = db.Column(db.Boolean, default=False)
    verification = db.Column(db.JSON)
    external_ref = db.Column(db.String(60))
    executed_at = db.Column(db.DateTime, default=utcnow)


class AutonomyRule(db.Model):
    __tablename__ = "autonomy_rule"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160))
    level = db.Column(db.Integer, default=1)           # 0..4
    action_types = db.Column(db.JSON, default=list)    # [] = all
    effect = db.Column(db.String(20), default="ALLOW_AUTO")   # ALLOW_AUTO / REQUIRE_APPROVAL / NEVER_AUTO
    conditions = db.Column(db.JSON, default=dict)      # max_cost, min_cost, min_confidence, criticality_in, industry, ...
    priority = db.Column(db.Integer, default=100)
    enabled = db.Column(db.Boolean, default=True)
    description = db.Column(db.String(300))


class AuditLog(db.Model):
    __tablename__ = "audit_log"
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=utcnow, index=True)
    actor = db.Column(db.String(60))
    category = db.Column(db.String(12), index=True)    # DATA / CALC / CONFIG / ACTION / APPROVAL / EXEC / SECURITY / SCENARIO
    entity_type = db.Column(db.String(30), index=True)
    entity_id = db.Column(db.String(60), index=True)
    event = db.Column(db.String(80))
    details = db.Column(db.JSON)
    trace = db.Column(db.JSON)                         # calc lineage: inputs, policy, formula, model, result


class Scenario(db.Model):
    __tablename__ = "scenario"
    id = db.Column(db.Integer, primary_key=True)
    scenario_no = db.Column(db.String(20), unique=True)
    name = db.Column(db.String(160))
    description = db.Column(db.Text)
    base_dataset = db.Column(db.String(160))
    created_by = db.Column(db.String(60))
    created_at = db.Column(db.DateTime, default=utcnow)
    time_step_days = db.Column(db.Integer, default=7)
    horizon_days = db.Column(db.Integer, default=90)
    runs = db.Column(db.Integer, default=40)
    seed = db.Column(db.Integer, default=42)
    assumptions = db.Column(db.JSON, default=dict)
    changes = db.Column(db.JSON, default=list)
    results = db.Column(db.JSON)
    status = db.Column(db.String(12), default="DRAFT")
    is_baseline = db.Column(db.Boolean, default=False)


class Experiment(db.Model):
    __tablename__ = "experiment"
    id = db.Column(db.Integer, primary_key=True)
    exp_no = db.Column(db.String(20), unique=True)
    name = db.Column(db.String(160))
    hypothesis = db.Column(db.Text)
    config = db.Column(db.JSON, default=dict)
    results = db.Column(db.JSON)
    status = db.Column(db.String(12), default="DRAFT")
    created_by = db.Column(db.String(60))
    created_at = db.Column(db.DateTime, default=utcnow)


class Event(db.Model):
    __tablename__ = "event"
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.String(64), unique=True, nullable=False)
    event_type = db.Column(db.String(40), index=True)
    source = db.Column(db.String(40))
    occurred_at = db.Column(db.DateTime, default=utcnow)
    received_at = db.Column(db.DateTime, default=utcnow)
    payload = db.Column(db.JSON)
    status = db.Column(db.String(12), default="RECEIVED")   # RECEIVED / PROCESSED / FAILED / DUPLICATE
    error = db.Column(db.String(400))


class SyncStatus(db.Model):
    __tablename__ = "sync_status"
    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(20), unique=True, nullable=False)   # ERP / WMS / TMS / 3PL / FORECAST / INVENTORY
    last_sync = db.Column(db.DateTime)
    expected_every_hours = db.Column(db.Float, default=24)
    records = db.Column(db.Integer, default=0)
    note = db.Column(db.String(200))


class Job(db.Model):
    __tablename__ = "job"
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.String(32), unique=True, nullable=False)
    kind = db.Column(db.String(30))
    status = db.Column(db.String(12), default="QUEUED")      # QUEUED / RUNNING / DONE / FAILED
    params = db.Column(db.JSON)
    result = db.Column(db.JSON)
    error = db.Column(db.String(400))
    created_at = db.Column(db.DateTime, default=utcnow)
    finished_at = db.Column(db.DateTime)


class Notification(db.Model):
    __tablename__ = "notification"
    id = db.Column(db.Integer, primary_key=True)
    channel = db.Column(db.String(12))
    subject = db.Column(db.String(200))
    body = db.Column(db.Text)
    status = db.Column(db.String(12))                        # SENT / SKIPPED / FAILED
    detail = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=utcnow)
