"""Reference/configuration seed data: states, roles, rules, KPI definitions, industry profiles, autonomy rules, policies.

Idempotent: safe to call at every start. It never overwrites values an administrator has edited.
"""
from __future__ import annotations

import os
import secrets

from werkzeug.security import generate_password_hash

from ..extensions import db
from ..models import (AutonomyRule, ControlPolicy, IndustryProfile, InventoryStateDef, KpiDefinition, ReplenishmentPolicy,
                      Role, Rule, SafetyStockPolicy, Setting, SyncStatus, User)
from ..utils.security import DEFAULT_ROLES
from . import settings_service as S


def seed_reference_data(create_users: bool = True) -> dict:
    n = {}
    n["states"] = _states()
    n["roles"] = _roles()
    n["rules"] = _rules()
    n["kpis"] = _kpis()
    n["profiles"] = _profiles()
    n["autonomy"] = _autonomy()
    n["policies"] = _policies()
    n["sync"] = _sync()
    if create_users:
        n["users"] = _users()
    if not Setting.query.filter_by(key="system.data_version").first():
        db.session.add(Setting(key="system.data_version", value=1))
    db.session.commit()
    S.bump_version()
    db.session.commit()
    return n


def _states() -> int:
    c = 0
    for code, label, cat, oh, al, pos, desc in S.DEFAULT_STATES:
        if not InventoryStateDef.query.filter_by(code=code).first():
            db.session.add(InventoryStateDef(code=code, label=label, category=cat, counts_on_hand=oh, allocatable=al,
                                             counts_in_position=pos, description=desc))
            c += 1
    return c


def _roles() -> int:
    c = 0
    desc = {"Executive": "Enterprise KPIs, scenario review, high-value approvals",
            "Supply Chain Manager": "Owns network policies, approvals, execution",
            "Demand Planner": "Forecast ingestion and consensus", "Inventory Planner": "Replenishment, allocation, alerts",
            "Procurement": "POs, supplier follow-up, expedites", "Warehouse Manager": "Stock accuracy, transfers, receipts",
            "Finance": "Working capital and cost approvals", "Operations": "Production and plant supply",
            "Administrator": "Configuration, users, rules, policies"}
    for name, perms in DEFAULT_ROLES.items():
        if not Role.query.filter_by(name=name).first():
            db.session.add(Role(name=name, permissions=perms, description=desc.get(name)))
            c += 1
    return c


def _users() -> int:
    """Named users for auth mode. Passwords come from env (ADMIN_PASSWORD) or are generated once and printed."""
    if User.query.count():
        return 0
    pwd = os.environ.get("ADMIN_PASSWORD")
    generated = False
    if not pwd:
        pwd, generated = secrets.token_urlsafe(9), True
    for name, role in [("admin", "Administrator"), ("planner", "Inventory Planner"), ("manager", "Supply Chain Manager"),
                       ("exec", "Executive"), ("demand", "Demand Planner"), ("procure", "Procurement"),
                       ("warehouse", "Warehouse Manager"), ("finance", "Finance"), ("ops", "Operations")]:
        db.session.add(User(username=name, name=name.title(), role=role, email=f"{name}@example.invalid",
                            password_hash=generate_password_hash(pwd)))
    if generated:
        print(f"[ICT] Generated password for all seeded users (set ADMIN_PASSWORD to control it): {pwd}")
    return 9


# ---------------------------------------------------------------------------------------------------------- rules
def _r(code, name, atype, cond, sev, action, desc, scope="pair", industries=None, suppress=0, escalate=48):
    return dict(code=code, name=name, alert_type=atype, condition=cond, severity=sev, recommended_action=action,
                description=desc, scope=scope, industries=industries or [], suppress_hours=suppress, escalate_after_hours=escalate)


RULES = [
    _r("STOCKOUT_RISK", "Projected stock-out", "STOCKOUT",
       {"any": [{"metric": "risk_rank", "op": ">=", "value": 2},
                {"all": [{"metric": "risk_rank", "op": ">=", "value": 1}, {"metric": "days_to_stockout", "op": "<=", "value": "lt_horizon"}, {"metric": "demand_daily", "op": ">", "value": 0}]}]},
       {"default": "MEDIUM", "when": [{"metric": "risk_rank", "op": ">=", "value": 2, "severity": "HIGH"},
                                      {"metric": "risk_rank", "op": ">=", "value": 3, "severity": "CRITICAL"}]},
       "REPLENISH", "IF stock-out probability is HIGH+, or MEDIUM+ with projected inventory below zero inside the replenishment window (lead time + review), THEN stock-out alert"),
    _r("SAFETY_STOCK_BREACH", "Safety stock breach", "SAFETY_STOCK_BREACH",
       {"all": [{"metric": "usable_on_hand", "op": "<", "value": "safety_stock"}, {"metric": "safety_stock", "op": ">", "value": 0},
                {"metric": "demand_daily", "op": ">", "value": 0}]},
       {"default": "MEDIUM", "when": [{"metric": "ss_ratio", "op": "<", "value": 0.5, "severity": "HIGH"}]},
       "REPLENISH", "IF usable stock < safety stock THEN safety-stock breach"),
    _r("EXCESS_INVENTORY", "Excess inventory", "EXCESS",
       {"all": [{"metric": "excess_value", "op": ">", "value": 250000}, {"metric": "inv_class", "op": "in", "value": ["EXCESS", "SLOW"]}]},
       {"default": "MEDIUM", "when": [{"metric": "excess_value", "op": ">", "value": 3000000, "severity": "HIGH"}]},
       "TRANSFER_STOCK", "IF stock > max level and > days-of-supply threshold THEN excess alert"),
    _r("OBSOLESCENCE", "Non-moving / obsolete stock", "OBSOLESCENCE",
       {"all": [{"metric": "inv_class", "op": "in", "value": ["NON_MOVING", "OBSOLETE"]}, {"metric": "on_hand_value", "op": ">", "value": 0}]},
       {"default": "MEDIUM", "when": [{"metric": "inv_class", "op": "==", "value": "OBSOLETE", "severity": "HIGH"}]},
       "REVIEW_OBSOLESCENCE", "IF no demand for the configured window THEN obsolescence review"),
    _r("AGING_STOCK", "Aged inventory", "AGING",
       {"all": [{"metric": "oldest_days", "op": ">", "value": 365}, {"metric": "on_hand_value", "op": ">", "value": 500000}]},
       {"default": "LOW"}, "REVIEW_OBSOLESCENCE", "IF inventory age > threshold THEN aging alert"),
    _r("EXPIRY_RISK", "Expiry approaching", "EXPIRY",
       {"any": [{"metric": "expiry_status", "op": "in", "value": ["NEAR", "CRITICAL", "EXPIRED"]}, {"metric": "at_risk_qty", "op": ">", "value": 0}]},
       {"default": "MEDIUM", "when": [{"metric": "expiry_status", "op": "in", "value": ["CRITICAL", "EXPIRED"], "severity": "CRITICAL"}]},
       "REVIEW_EXPIRY", "IF expiry < threshold THEN expiry alert"),
    _r("PO_DELAY", "Purchase order overdue", "PO_DELAY",
       {"all": [{"metric": "overdue_days", "op": ">", "value": 0}, {"metric": "kind", "op": "==", "value": "PO"}]},
       {"default": "MEDIUM", "when": [{"metric": "overdue_days", "op": ">", "value": 7, "severity": "HIGH"}]},
       "EXPEDITE_PO", "IF PO promised date has passed and it is not received THEN PO-delay alert", scope="supply"),
    _r("ETA_CHANGE", "ETA later than promised", "ETA_CHANGE",
       {"all": [{"metric": "eta_slip_days", "op": ">", "value": 2}]},
       {"default": "MEDIUM", "when": [{"metric": "eta_slip_days", "op": ">", "value": 7, "severity": "HIGH"}]},
       "EXPEDITE_PO", "IF supplier ETA > promised date THEN supplier-delay / ETA-change alert", scope="supply"),
    _r("SUPPLIER_DELAY", "Supplier performance deterioration", "SUPPLIER_DELAY",
       {"any": [{"metric": "otif", "op": "<", "value": 0.85}, {"metric": "lt_reliability", "op": "<", "value": 0.7}]},
       {"default": "MEDIUM", "when": [{"metric": "otif", "op": "<", "value": 0.7, "severity": "HIGH"}]},
       "REVIEW_SUPPLIER", "IF supplier OTIF or lead-time reliability falls below threshold THEN supplier alert", scope="supplier"),
    _r("LEAD_TIME_INCREASE", "Observed lead time above static", "LEAD_TIME_INCREASE",
       {"all": [{"metric": "lt_ratio", "op": ">", "value": 1.25}, {"metric": "lt_obs_n", "op": ">=", "value": 8}]},
       {"default": "MEDIUM", "when": [{"metric": "lt_ratio", "op": ">", "value": 1.6, "severity": "HIGH"}]},
       "CHANGE_SAFETY_STOCK", "IF observed lead time exceeds the master lead time by > 25 % THEN alert"),
    _r("INVENTORY_VARIANCE", "Inventory variance vs source system", "INVENTORY_VARIANCE",
       {"all": [{"metric": "variance_pct_abs", "op": ">", "value": 0.02}, {"metric": "variance_value", "op": ">", "value": 5000}, {"metric": "recon_status", "op": "in", "value": ["VARIANCE", "MISSING_IN_ICT", "MISSING_IN_SOURCE", "NEGATIVE", "UNIT_MISMATCH", "DUPLICATE", "LOCATION_MISMATCH"]}]},
       {"default": "MEDIUM", "when": [{"metric": "variance_pct_abs", "op": ">", "value": 0.1, "severity": "HIGH"}]},
       "CYCLE_COUNT", "IF ICT and source balances differ beyond tolerance THEN variance alert", scope="recon"),
    _r("DEMAND_SPIKE", "Demand spike", "DEMAND_SPIKE",
       {"all": [{"metric": "demand_ratio_4w", "op": ">", "value": 1.5}, {"metric": "demand_z", "op": ">", "value": 2.0}]},
       {"default": "MEDIUM", "when": [{"metric": "demand_ratio_4w", "op": ">", "value": 2.5, "severity": "HIGH"}]},
       "CHANGE_SAFETY_STOCK", "IF the last 4 weeks are > 1.5× the prior mean (z > 2) THEN demand-spike alert"),
    _r("DEMAND_DROP", "Demand drop", "DEMAND_DROP",
       {"all": [{"metric": "demand_ratio_4w", "op": "<", "value": 0.5}, {"metric": "demand_z", "op": "<", "value": -2.0}]},
       {"default": "LOW"}, "REVIEW_OBSOLESCENCE", "IF the last 4 weeks are < 0.5× the prior mean THEN demand-drop alert"),
    _r("CAPACITY_BREACH", "Storage capacity breach", "CAPACITY_BREACH",
       {"all": [{"metric": "utilization", "op": ">", "value": 0.95}]},
       {"default": "MEDIUM", "when": [{"metric": "utilization", "op": ">", "value": 1.05, "severity": "HIGH"}]},
       "TRANSFER_STOCK", "IF node utilisation > 95 % of capacity THEN capacity alert", scope="location"),
    _r("ALLOCATION_CONFLICT", "Allocation conflict", "ALLOCATION_CONFLICT",
       {"any": [{"metric": "over_allocated", "op": "==", "value": True}, {"metric": "unallocated_due_in_lt", "op": ">", "value": "available_pos"}]},
       {"default": "MEDIUM", "when": [{"metric": "over_allocated", "op": "==", "value": True, "severity": "HIGH"}]},
       "ALLOCATE_STOCK", "IF claims exceed usable stock, or due demand exceeds availability THEN allocation conflict"),
    _r("QUALITY_HOLD", "Extended quality hold", "QUALITY_HOLD",
       {"all": [{"metric": "quality_status", "op": "in", "value": ["QUARANTINE", "BLOCKED"]}, {"metric": "hold_days", "op": ">", "value": 7}]},
       {"default": "MEDIUM"}, "RELEASE_STOCK", "IF a lot stays in quarantine/blocked beyond 7 days THEN quality-hold alert", scope="lot"),
    _r("DATA_STALE", "Stale source data", "DATA_STALE",
       {"all": [{"metric": "age_ratio", "op": ">", "value": 1.0}]},
       {"default": "MEDIUM", "when": [{"metric": "age_ratio", "op": ">", "value": 3.0, "severity": "HIGH"}]},
       "CHECK_INTEGRATION", "IF source last-sync age exceeds its expected frequency THEN stale-data alert", scope="sync"),
    _r("ECO_OLD_REVISION", "Old-revision stock after ECO", "OBSOLESCENCE",
       {"all": [{"metric": "eco_leftover_qty", "op": ">", "value": 0}]},
       {"default": "HIGH"}, "REVIEW_OBSOLESCENCE", "IF stock of a superseded revision exceeds what can be consumed before the ECO cut-over THEN review (never auto-scrap)",
       industries=["AUTOMOTIVE"]),
    _r("EOL_RUNOUT", "End-of-life runout / obsolescence exposure", "OBSOLESCENCE",
       {"all": [{"metric": "eol_exposure_value", "op": ">", "value": 0}]},
       {"default": "MEDIUM", "when": [{"metric": "eol_exposure_value", "op": ">", "value": 500000, "severity": "HIGH"}]},
       "REVIEW_OBSOLESCENCE", "IF stock exceeds forecast consumption until the EOL date THEN obsolescence exposure alert",
       industries=["ELECTRONICS"]),
]


def _rules() -> int:
    c = 0
    for r in RULES:
        if Rule.query.filter_by(code=r["code"]).first():
            continue
        db.session.add(Rule(code=r["code"], name=r["name"], description=r["description"], alert_type=r["alert_type"],
                            scope=r["scope"], condition=r["condition"], severity=r["severity"], industries=r["industries"],
                            recommended_action=r["recommended_action"], suppress_hours=r["suppress_hours"],
                            escalate_after_hours=r["escalate_after_hours"]))
        c += 1
    return c


# ---------------------------------------------------------------------------------------------------------- KPIs
KPIS = [
    # code, name, category, formula, unit, direction, window, watch, attention, critical, description
    ("inventory_turns", "Inventory Turns", "Efficiency", "cogs_annual / avg_inventory_value", "ratio", "higher", 365, 6, 4, 2,
     "Annualised COGS ÷ average inventory value"),
    ("dio", "Days Inventory Outstanding (DIO)", "Efficiency", "365 * avg_inventory_value / cogs_annual", "days", "lower", 365, 60, 90, 150,
     "365 × average inventory value ÷ annualised COGS"),
    ("days_of_supply", "Days of Supply", "Availability", "on_hand_value / (cogs_annual / 365)", "days", "lower", 90, 60, 90, 150,
     "Current on-hand value ÷ daily COGS"),
    ("service_level", "Service Level (line, cycle)", "Service", "lines_in_full / lines_total", "pct", "higher", 90, 0.95, 0.90, 0.80,
     "Share of order lines filled in full at first shipment"),
    ("fill_rate", "Fill Rate (units)", "Service", "units_shipped / units_ordered", "pct", "higher", 90, 0.97, 0.93, 0.85,
     "Units shipped ÷ units ordered"),
    ("otif", "OTIF", "Service", "lines_otif / lines_total", "pct", "higher", 90, 0.92, 0.85, 0.75,
     "Lines delivered on time AND in full ÷ lines"),
    ("stockout_rate", "Stock-out Rate", "Availability", "stockout_pairs / demand_pairs", "pct", "lower", 30, 0.03, 0.08, 0.15,
     "SKU-locations with demand and no usable stock ÷ SKU-locations with demand"),
    ("backorder_rate", "Backorder Rate", "Service", "units_backordered / units_ordered", "pct", "lower", 90, 0.02, 0.05, 0.10,
     "Backordered units ÷ ordered units"),
    ("carrying_cost", "Carrying Cost (annual)", "Financial", "avg_inventory_value * holding_rate", "money", "lower", 365, None, None, None,
     "Average inventory value × configured holding-cost rate"),
    ("inventory_accuracy", "Inventory Accuracy", "Quality", "accuracy_matched / accuracy_total", "pct", "higher", 90, 0.97, 0.93, 0.85,
     "Records within tolerance vs physical count ÷ records counted"),
    ("excess_pct", "Excess %", "Efficiency", "excess_value / inventory_value", "pct", "lower", 30, 0.10, 0.20, 0.35, "Excess value ÷ inventory value"),
    ("slow_pct", "Slow-Moving %", "Efficiency", "slow_value / inventory_value", "pct", "lower", 30, 0.08, 0.15, 0.25, "Slow-moving value ÷ inventory value"),
    ("obsolete_pct", "Obsolete %", "Risk", "obsolete_value / inventory_value", "pct", "lower", 30, 0.02, 0.05, 0.10, "Obsolete value ÷ inventory value"),
    ("forecast_bias", "Forecast Bias", "Quality", "forecast_minus_actual / actual_sum", "pct", "lower", 90, 0.05, 0.10, 0.20,
     "Σ(forecast − actual) ÷ Σ actual  (positive = over-forecast; absolute value is judged)"),
    ("supplier_otif", "Supplier OTIF", "Service", "supplier_lines_otif / supplier_lines_total", "pct", "higher", 180, 0.92, 0.85, 0.75,
     "Inbound PO lines received on time AND in full"),
    ("lt_variability", "Lead-Time Variability (CV)", "Risk", "lt_std / lt_mean", "ratio", "lower", 180, 0.2, 0.35, 0.5, "σ ÷ mean of observed lead times"),
    ("lt_reliability", "Lead-Time Reliability", "Risk", "lt_on_time / lt_total", "pct", "higher", 180, 0.9, 0.8, 0.65, "Receipts within promised lead time (+1 d)"),
    ("order_cycle_time", "Order Cycle Time", "Service", "order_cycle_days_sum / order_cycle_count", "days", "lower", 90, None, None, None,
     "Average days from order to delivery"),
    ("stockout_risk", "Stock-out Risk (weighted)", "Risk", "risk_weighted / risk_weight", "pct", "lower", 30, 0.05, 0.10, 0.20,
     "Demand-value-weighted average stock-out probability over lead time"),
    ("working_capital", "Working Capital in Inventory", "Financial", "inventory_value + in_transit_value", "money", "lower", 30, None, None, None,
     "On-hand + owned in-transit inventory value"),
    ("excess_capital", "Excess Capital", "Financial", "excess_value", "money", "lower", 30, None, None, None, "Value of stock beyond policy maximum"),
    ("lost_sales_exposure", "Potential Lost Sales", "Financial", "lost_sales_value", "money", "lower", 30, None, None, None,
     "Expected shortage × lost-sale share × price over the lead time"),
]


def _kpis() -> int:
    c = 0
    for code, name, cat, formula, unit, direction, win, w, a, cr, desc in KPIS:
        if KpiDefinition.query.filter_by(code=code).first():
            continue
        db.session.add(KpiDefinition(code=code, name=name, category=cat, formula=formula, unit=unit, direction=direction,
                                     window_days=win, watch=w, attention=a, critical=cr, alert_threshold=cr, description=desc))
        c += 1
    return c


# ---------------------------------------------------------------------------------------------------------- profiles
PROFILES = {
    "GENERAL": dict(name="General (all-industry)", description="Neutral defaults: no industry-specific rules.",
                    terminology={}, kpis=["inventory_turns", "dio", "service_level", "fill_rate", "excess_pct", "stockout_risk"],
                    rules=[], dashboards=["network", "position", "heatmap", "incidents", "actions"],
                    alerts=["STOCKOUT", "EXCESS", "PO_DELAY"], policies={}, focus=["Visibility", "Replenishment", "Risk"],
                    recommended_actions=["CREATE_PO", "TRANSFER_STOCK", "CHANGE_SAFETY_STOCK"]),
    "AUTOMOTIVE": dict(name="Automotive", description="JIT/JIS supply to assembly plants, line-side stock, ECO and BOM revision control.",
                       terminology={"SKU": "Part number", "Location": "Plant / line-side", "Supplier": "Tier-1/2 supplier", "UNRESTRICTED": "Released"},
                       kpis=["service_level", "otif", "supplier_otif", "lt_variability", "stockout_risk", "dio"],
                       rules=["ECO_OLD_REVISION", "LEAD_TIME_INCREASE"], dashboards=["line_side", "supplier_tiers", "eco", "plant_shutdown_risk"],
                       alerts=["STOCKOUT", "ETA_CHANGE", "SUPPLIER_DELAY", "OBSOLESCENCE"],
                       policies={"engine.lt_basis": "p90", "excess.dos_threshold": 30, "engine.service_level": 0.99},
                       focus=["JIT", "JIS", "Line-side stock", "Critical components", "Supplier tiers", "ECO", "BOM revision", "Plant shutdown risk"],
                       recommended_actions=["EXPEDITE_PO", "TRANSFER_STOCK", "REVIEW_OBSOLESCENCE", "CHANGE_SAFETY_STOCK"]),
    "PHARMA": dict(name="Pharma / Life Sciences", description="Lot/batch control, FEFO, expiry, cold chain, quarantine and quality release.",
                   terminology={"SKU": "Product", "lot": "Batch", "UNRESTRICTED": "Released", "QUARANTINED": "Quarantine"},
                   kpis=["service_level", "inventory_accuracy", "obsolete_pct", "dio", "stockout_risk"],
                   rules=["EXPIRY_RISK", "QUALITY_HOLD"], dashboards=["expiry", "fefo", "cold_chain", "quality_release", "pedigree"],
                   alerts=["EXPIRY", "QUALITY_HOLD", "STOCKOUT"],
                   policies={"thresholds.expiry": {"near_days": 120, "critical_days": 45, "near_pct": 0.4}, "excess.dos_threshold": 120,
                             "engine.service_level": 0.99},
                   focus=["Lot", "Batch", "Expiry", "FEFO", "Cold chain", "Quarantine", "Quality release", "Pedigree"],
                   recommended_actions=["REVIEW_EXPIRY", "TRANSFER_STOCK", "RELEASE_STOCK", "QUARANTINE"]),
    "RETAIL_FMCG": dict(name="Retail / FMCG", description="Omnichannel store/DC/dark-store inventory, promotions, seasonality, markdown and shelf life.",
                        terminology={"SKU": "Article", "Location": "Store / DC", "Supplier": "Vendor"},
                        kpis=["fill_rate", "service_level", "excess_pct", "slow_pct", "inventory_turns", "dio"],
                        rules=["EXPIRY_RISK", "DEMAND_SPIKE"], dashboards=["omnichannel", "promotions", "markdown", "shelf_availability"],
                        alerts=["STOCKOUT", "EXPIRY", "DEMAND_SPIKE", "EXCESS"],
                        policies={"excess.dos_threshold": 45, "thresholds.expiry": {"near_days": 30, "critical_days": 10, "near_pct": 0.3},
                                  "engine.service_level": 0.97},
                        focus=["Omnichannel", "Store inventory", "DC", "Micro-fulfilment", "Promotion", "Seasonality", "Markdown", "Shelf life", "Returns"],
                        recommended_actions=["TRANSFER_STOCK", "CREATE_PO", "REVIEW_EXPIRY", "ALLOCATE_STOCK"]),
    "HIGH_TECH": dict(name="High Tech / Electronics", description="Component shortage, obsolescence, lifecycle/EOL, alternates and lead-time volatility.",
                      terminology={"SKU": "Component / MPN", "Supplier": "EMS/ODM / distributor"},
                      kpis=["obsolete_pct", "lt_variability", "supplier_otif", "stockout_risk", "excess_pct"],
                      rules=["EOL_RUNOUT", "LEAD_TIME_INCREASE"], dashboards=["lifecycle", "alternates", "obsolescence"],
                      alerts=["STOCKOUT", "OBSOLESCENCE", "LEAD_TIME_INCREASE"],
                      policies={"excess.dos_threshold": 60, "excess.obsolescence_prob": {"EXCESS": 0.15, "SLOW": 0.30, "NON_MOVING": 0.6, "OBSOLETE": 1.0}},
                      focus=["Obsolescence", "Component shortage", "BOM alternates", "Lifecycle", "EMS/ODM", "Lead-time volatility"],
                      recommended_actions=["CREATE_PO", "ALLOCATE_STOCK", "REVIEW_OBSOLESCENCE", "EXPEDITE_PO"]),
    "MANUFACTURING": dict(name="Manufacturing", description="Raw material, WIP, production orders, BOM/MRP and capacity.",
                          terminology={"SKU": "Material", "Location": "Plant / store-room"},
                          kpis=["inventory_turns", "dio", "stockout_risk", "supplier_otif", "excess_pct"],
                          rules=["LEAD_TIME_INCREASE"], dashboards=["wip", "mrp", "capacity"], alerts=["STOCKOUT", "PO_DELAY", "CAPACITY_BREACH"],
                          policies={}, focus=["Raw material", "WIP", "Production orders", "BOM", "MRP", "Capacity"],
                          recommended_actions=["CREATE_PO", "EXPEDITE_PO", "CHANGE_REORDER_POINT"]),
    "SPARE_PARTS": dict(name="Spare Parts / Aftermarket", description="Intermittent demand, criticality-driven service levels, long lead times, non-moving stock.",
                        terminology={"SKU": "Spare part"},
                        kpis=["service_level", "obsolete_pct", "slow_pct", "lt_variability", "dio"],
                        rules=["OBSOLESCENCE"], dashboards=["intermittent", "criticality", "non_moving"], alerts=["STOCKOUT", "OBSOLESCENCE", "LEAD_TIME_INCREASE"],
                        policies={"excess.dos_threshold": 365, "excess.nonmoving_days": 365, "excess.obsolete_days": 730,
                                  "engine.ss_method": "empirical"},
                        focus=["Intermittent demand", "Criticality", "Service level", "Long lead times", "Non-moving stock"],
                        recommended_actions=["CREATE_PO", "CHANGE_SAFETY_STOCK", "REVIEW_OBSOLESCENCE"]),
}


def _profiles() -> int:
    c = 0
    for code, p in PROFILES.items():
        if IndustryProfile.query.filter_by(code=code).first():
            continue
        db.session.add(IndustryProfile(code=code, name=p["name"], description=p["description"], terminology=p["terminology"],
                                       kpis=p["kpis"], rules=p["rules"], dashboards=p["dashboards"], alerts=p["alerts"],
                                       policies=p["policies"], recommended_actions=p["recommended_actions"], focus=p["focus"]))
        c += 1
    return c


# ---------------------------------------------------------------------------------------------------------- autonomy
def _autonomy() -> int:
    if AutonomyRule.query.count():
        return 0
    rules = [
        AutonomyRule(name="Auto-approve transfers below ₹50,000", level=3, action_types=["TRANSFER_STOCK"], effect="ALLOW_AUTO",
                     conditions={"max_cost": 50000, "min_confidence": 0.6, "criticality_not_in": ["Critical"]}, priority=50,
                     description="Level 3: auto-execute within guardrails (simulation-only until a live connector is configured)."),
        AutonomyRule(name="Require approval for purchases above ₹500,000", level=2, action_types=["CREATE_PO", "EXPEDITE_PO"],
                     effect="REQUIRE_APPROVAL", conditions={"min_cost": 500000}, priority=20,
                     description="High-value purchasing always needs a manager/executive."),
        AutonomyRule(name="Never auto-execute pharma batch release", level=1, action_types=["RELEASE_STOCK"], effect="NEVER_AUTO",
                     conditions={"industry": "PHARMA"}, priority=1, description="Quality release is a regulated human decision."),
        AutonomyRule(name="Never auto-execute actions on critical components", level=1, action_types=[], effect="NEVER_AUTO",
                     conditions={"criticality_in": ["Critical"]}, priority=2, description="Critical parts always keep a human in the loop."),
        AutonomyRule(name="Never auto-execute quarantine / obsolescence / scrap decisions", level=1,
                     action_types=["QUARANTINE", "REVIEW_OBSOLESCENCE", "RELEASE_STOCK"], effect="NEVER_AUTO", conditions={}, priority=3,
                     description="Irreversible or quality-related: human only. ICT never auto-scraps inventory."),
    ]
    db.session.add_all(rules)
    return len(rules)


# ---------------------------------------------------------------------------------------------------------- policies
def _policies() -> int:
    c = 0
    if not SafetyStockPolicy.query.count():
        db.session.add_all([
            SafetyStockPolicy(scope_level="GLOBAL", scope_key="*", params={"method": "combined", "service_level": 0.95}, notes="Enterprise default"),
            SafetyStockPolicy(scope_level="INDUSTRY", scope_key="AUTOMOTIVE", params={"method": "combined", "service_level": 0.99}, notes="Line-stop cost is high"),
            SafetyStockPolicy(scope_level="INDUSTRY", scope_key="PHARMA", params={"method": "periodic", "service_level": 0.99}, notes="Patient safety"),
            SafetyStockPolicy(scope_level="INDUSTRY", scope_key="FMCG", params={"method": "periodic", "service_level": 0.97}),
            SafetyStockPolicy(scope_level="INDUSTRY", scope_key="RETAIL", params={"method": "periodic", "service_level": 0.96}),
            SafetyStockPolicy(scope_level="INDUSTRY", scope_key="ELECTRONICS", params={"method": "combined", "service_level": 0.95}),
            SafetyStockPolicy(scope_level="INDUSTRY", scope_key="SPARE_PARTS", params={"method": "empirical", "service_level": 0.92}),
            SafetyStockPolicy(scope_level="INDUSTRY", scope_key="MANUFACTURING", params={"method": "combined", "service_level": 0.96}),
        ])
        c += 8
    if not ReplenishmentPolicy.query.count():
        db.session.add(ReplenishmentPolicy(scope_level="GLOBAL", scope_key="*", params={"policy": "ROP"}, notes="Enterprise default (s,Q)"))
        c += 1
    if not ControlPolicy.query.count():
        db.session.add_all([
            ControlPolicy(policy_type="allocation", scope_level="GLOBAL", scope_key="*",
                          params={"method": "weighted", "weights": {"priority_customer": 0.30, "revenue": 0.20, "margin": 0.10,
                                                                   "criticality": 0.10, "service_level": 0.10, "geography": 0.05,
                                                                   "contract": 0.15, "fifo": 0.0}, "require_released": True},
                          notes="Composite allocation score"),
            ControlPolicy(policy_type="allocation", scope_level="INDUSTRY", scope_key="PHARMA",
                          params={"method": "fefo_priority", "require_released": True}, notes="Only released stock is allocatable"),
            ControlPolicy(policy_type="allocation", scope_level="INDUSTRY", scope_key="FMCG",
                          params={"method": "fifo", "require_released": False}),
            ControlPolicy(policy_type="transfer", scope_level="GLOBAL", scope_key="*",
                          params={"min_transfer_qty": 1, "keep_safety_stock_at_donor": True, "max_transfer_days": 10}),
            ControlPolicy(policy_type="expiry", scope_level="GLOBAL", scope_key="*", params={"min_remaining_pct_for_allocation": 0.0}),
            ControlPolicy(policy_type="expiry", scope_level="INDUSTRY", scope_key="PHARMA", params={"min_remaining_days_for_allocation": 30}),
        ])
        c += 6
    return c


def _sync() -> int:
    c = 0
    for src, hrs in [("ERP", 24), ("WMS", 4), ("TMS", 6), ("3PL", 24), ("FORECAST", 168), ("INVENTORY", 24)]:
        if not SyncStatus.query.filter_by(source=src).first():
            db.session.add(SyncStatus(source=src, last_sync=None, expected_every_hours=hrs, note="never synced"))
            c += 1
    return c
