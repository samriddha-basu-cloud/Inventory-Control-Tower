"""Settings: engine parameters, thresholds, weights, roles, KPI definitions, rules, inventory states."""
from __future__ import annotations

import json

from flask import Blueprint, render_template, request

from ..extensions import db
from ..models import InventoryStateDef, KpiDefinition, Role, Rule, User
from ..rules.rule_engine import eval_condition
from ..services import audit_service, kpi_service, settings_service as S
from ..utils.security import PERMISSIONS, require
from . import helpers as H

bp = Blueprint("settings", __name__)

# key, label, kind, help, choices
FIELDS = [
    ("Engine", [
        ("engine.service_level", "Default cycle service level", "float", "0.5–0.9999 · used when no policy/item target exists", None),
        ("engine.ss_method", "Default safety-stock method", "choice", "Any of the eight methodologies", ["basic", "demand", "leadtime", "combined", "service_level", "periodic", "continuous", "empirical"]),
        ("engine.use_observed_lt", "Use observed lead-time distributions", "bool", "When on, observed history overrides the static master lead time in risk & safety stock", None),
        ("engine.lt_basis", "Lead-time basis", "choice", "static · observed mean · P50 · P90 (P90 sets σL = 0 to avoid double counting)", ["static", "observed_mean", "p50", "p90"]),
        ("engine.min_lt_obs", "Minimum lead-time observations", "int", "Below this the distribution is not trusted", None),
        ("engine.holding_rate", "Annual holding-cost rate", "float", "Capital + storage + service + risk (0.22 = 22 %)", None),
        ("engine.ordering_cost", "Ordering cost per order", "float", "Used by EOQ and optimisation", None),
        ("engine.review_days", "Default review period (days)", "float", "Periodic-review protection interval", None),
        ("engine.lost_sale_fraction", "Share of unmet demand that is lost", "float", "The rest is backordered", None),
        ("engine.horizon_weeks", "Projection horizon (weeks)", "int", "Time-phased projection length", None),
    ]),
    ("Excess & obsolescence", [
        ("excess.dos_threshold", "Excess days-of-supply threshold", "float", "Stock above max level AND this many days is excess", None),
        ("excess.slow_days", "Slow-moving (days since movement)", "float", "", None), ("excess.nonmoving_days", "Non-moving (days without demand)", "float", "", None),
        ("excess.obsolete_days", "Obsolete (days without demand)", "float", "", None),
        ("excess.obsolescence_prob", "Obsolescence probability by class", "json", "EXCESS / SLOW / NON_MOVING / OBSOLETE", None),
    ]),
    ("Thresholds", [
        ("thresholds.stockout", "Stock-out risk bands (probability)", "json", "{'medium':0.05,'high':0.15,'critical':0.35}", None),
        ("thresholds.expiry", "Expiry thresholds", "json", "near_days / critical_days / near_pct", None),
        ("aging.buckets", "Aging bucket edges (days)", "json", "e.g. [30,60,90,180,365] · custom buckets allowed", None),
        ("abc.thresholds", "ABC cumulative cut-offs", "json", "{'A':0.8,'B':0.95} → A top 80 %, B next 15 %, C rest", None),
        ("xyz.thresholds", "XYZ CV cut-offs", "json", "{'X':0.5,'Y':1.0}", None), ("fsn.thresholds", "FSN thresholds", "json", "", None), ("hml.thresholds", "HML thresholds", "json", "", None),
        ("alerts.min_priority", "Suppress LOW alerts below priority", "float", "Alert-fatigue control", None),
        ("thresholds.risk_exposure", "Risk severity by exposure", "json", "{'medium':50000,'high':500000,'critical':2000000}", None),
    ]),
    ("Weights", [
        ("weights.priority", "Planner Priority Score weights", "json", "financial, service, production, customer, severity, time", None),
        ("weights.health", "Health Index weights", "json", "", None), ("weights.supplier_risk", "Supplier risk weights", "json", "", None), ("weights.confidence", "Confidence weights", "json", "", None),
        ("optimization.weights", "Optimisation objective weights", "json", "stockout, holding, expedite, carbon, working_capital, obsolescence", None),
        ("optimization.budget", "Optimisation budget", "float", "", None), ("optimization.carbon_price", "Internal carbon price (per kg CO2e)", "float", "Shadow price used to rank options - not a market price", None),
    ]),
    ("Reconciliation, S&OP & network", [
        ("recon.rules", "Reconciliation rules", "json", "tolerance_pct, tolerance_abs, stale_hours, checks[]", None), ("sop.upside_pct", "S&OP upside %", "float", "", None), ("sop.downside_pct", "S&OP downside %", "float", "", None),
        ("transfer.policy", "Transfer policy", "json", "keep_safety_stock_at_donor, max_transfer_days, min_transfer_qty, restricted_lanes[]", None),
        ("carbon.factors", "Emission factors (kg CO2e / tonne-km)", "json", "Estimates - use certified factors for reporting", None), ("freight.rate_per_tkm", "Freight rate per tonne-km", "json", "", None),
    ]),
]
ALL_KEYS = {f[0]: f for grp in FIELDS for f in grp[1]}


@bp.route("/settings")
def home():
    groups = []
    for title, fields in FIELDS:
        items = []
        for key, label, kind, help_, choices in fields:
            v = S.get(key)
            items.append({"key": key, "label": label, "kind": kind, "help": help_, "choices": choices, "value": json.dumps(v) if kind == "json" else v, "source": S.get_source(key)})
        groups.append((title, items))
    return render_template("settings/home.html", groups=groups, roles=Role.query.order_by(Role.name).all(), perms=PERMISSIONS, kpis=KpiDefinition.query.order_by(KpiDefinition.category, KpiDefinition.code).all(),
                           rules=Rule.query.order_by(Rule.code).all(), states=InventoryStateDef.query.order_by(InventoryStateDef.category, InventoryStateDef.id).all(), users=User.query.order_by(User.username).all(),
                           measures=kpi_service.MEASURE_DOCS, industry=S.active_industry())


def _parse(kind, raw):
    if kind == "bool":
        return raw in ("on", "1", "true", "True")
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind == "json":
        return json.loads(raw)
    return raw


@bp.route("/settings/save", methods=["POST"])
@require("configure")
def save():
    key = request.form.get("key", "")
    if key not in ALL_KEYS:
        H.err("Unknown setting.")
        return H.back("settings.home")
    _, label, kind, _, choices = ALL_KEYS[key]
    try:
        if request.form.get("reset"):
            S.reset_value(key, actor=H.actor())
            db.session.commit()
            H.ok(f"{label} reset to default.")
            return H.back("settings.home")
        raw = request.form.get("value", "")
        val = _parse(kind, raw if kind != "bool" else request.form.get("value", "off"))
        if choices and val not in choices:
            raise ValueError(f"must be one of {choices}")
        if key == "engine.service_level" and not 0.5 <= val < 1:
            raise ValueError("service level must be in [0.5, 1)")
        if kind in ("float", "int") and val < 0:
            raise ValueError("must not be negative")
        if key == "aging.buckets" and not (isinstance(val, list) and all(isinstance(x, (int, float)) and x > 0 for x in val) and len(val) >= 1):
            raise ValueError("bucket edges must be a list of positive numbers")
        if kind == "json" and isinstance(val, dict) and key in ("weights.priority", "weights.health", "weights.supplier_risk", "weights.confidence") and any(float(x) < 0 for x in val.values()):
            raise ValueError("weights must be non-negative")
        S.set_value(key, val, actor=H.actor())
        db.session.commit()
        H.ok(f"{label} saved. All dashboards and engines use the new value immediately.")
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        db.session.rollback()
        H.err(f"{label} not saved: {e}")
    return H.back("settings.home")


@bp.route("/settings/role/<name>", methods=["POST"])
@require("admin")
def save_role(name):
    r = Role.query.filter_by(name=name).first_or_404()
    perms = [p for p in PERMISSIONS if request.form.get(f"perm_{p}")]
    if name == "Administrator" and "admin" not in perms:
        H.err("The Administrator role must keep the admin permission.")
        return H.back("settings.home")
    old = list(r.permissions or [])
    r.permissions = perms
    audit_service.log("CONFIG", "Role", name, "permissions_changed", {"old": old, "new": perms}, actor=H.actor())
    S.bump_version()
    db.session.commit()
    H.ok(f"Permissions for {name} updated.")
    return H.back("settings.home")


@bp.route("/settings/kpi/<code>", methods=["POST"])
@require("configure")
def save_kpi(code):
    k = KpiDefinition.query.filter_by(code=code).first_or_404()
    formula = (request.form.get("formula") or "").strip()
    ok, msg = kpi_service.validate_formula(formula)
    if not ok:
        H.err(f"Formula rejected: {msg}")
        return H.back("settings.home")
    old = {"formula": k.formula, "watch": k.watch, "attention": k.attention, "critical": k.critical, "window": k.window_days}
    k.formula = formula
    for f, attr in (("watch", "watch"), ("attention", "attention"), ("critical", "critical"), ("alert_threshold", "alert_threshold")):
        v = H.fnum(request.form.get(f))
        setattr(k, attr, v)
    k.window_days = int(H.fnum(request.form.get("window_days"), k.window_days or 90))
    k.direction = request.form.get("direction") if request.form.get("direction") in ("higher", "lower") else k.direction
    k.entity_level = request.form.get("entity_level") or k.entity_level
    k.enabled = bool(request.form.get("enabled"))
    audit_service.log("CONFIG", "KpiDefinition", code, "kpi_changed", {"old": old, "new": {"formula": k.formula, "watch": k.watch, "attention": k.attention, "critical": k.critical}}, actor=H.actor())
    S.bump_version()
    db.session.commit()
    H.ok(f"KPI {k.name} updated.")
    return H.back("settings.home")


@bp.route("/settings/rule/<code>", methods=["POST"])
@require("configure")
def save_rule(code):
    r = Rule.query.filter_by(code=code).first_or_404()
    try:
        if request.form.get("condition"):
            cond = json.loads(request.form["condition"])
            eval_condition(cond, {})     # structural validation (unknown operators raise)
            if cond != r.condition:
                r.condition, r.version = cond, (r.version or 1) + 1
        if request.form.get("severity"):
            r.severity = json.loads(request.form["severity"])
        r.enabled = bool(request.form.get("enabled"))
        r.suppress_hours = int(H.fnum(request.form.get("suppress_hours"), r.suppress_hours or 0))
        r.escalate_after_hours = int(H.fnum(request.form.get("escalate_after_hours"), r.escalate_after_hours or 48))
        audit_service.log("CONFIG", "Rule", code, "rule_changed", {"enabled": r.enabled, "version": r.version}, actor=H.actor())
        S.bump_version()
        db.session.commit()
        H.ok(f"Rule {code} saved (v{r.version}). Run detection to apply.")
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        db.session.rollback()
        H.err(f"Rule not saved: {e}")
    return H.back("settings.home")


@bp.route("/settings/state/<code>", methods=["POST"])
@require("configure")
def save_state(code):
    s = InventoryStateDef.query.filter_by(code=code).first_or_404()
    if s.category == "PHYSICAL":
        s.counts_on_hand = bool(request.form.get("counts_on_hand"))
        s.allocatable = bool(request.form.get("allocatable"))
        s.counts_in_position = bool(request.form.get("counts_in_position"))
    s.label = (request.form.get("label") or s.label)[:60]
    s.enabled = bool(request.form.get("enabled"))
    audit_service.log("CONFIG", "InventoryStateDef", code, "state_changed", {"on_hand": s.counts_on_hand, "allocatable": s.allocatable}, actor=H.actor())
    S.bump_version()
    db.session.commit()
    H.ok(f"Inventory state {code} updated: available/position formulas follow immediately.")
    return H.back("settings.home")
