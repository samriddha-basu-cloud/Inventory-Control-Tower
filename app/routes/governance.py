"""Model / rule governance, experiments, audit trail."""
from __future__ import annotations

from flask import Blueprint, render_template, request

from ..extensions import db
from ..models import AuditLog, Experiment, KpiDefinition, Rule
from ..services import experiment_service as ex, health_service, replenishment_service, safety_stock_service, settings_service as S, simulation_service
from ..utils.security import require
from . import helpers as H

bp = Blueprint("governance", __name__)

MODEL_REGISTRY = [
    ("Safety stock (8 methods)", "1.0", "services/safety_stock_service.py", "Basic max-avg · demand · lead-time · combined · fill-rate (Type-2) · periodic · continuous · empirical bootstrap", "Per policy scope"),
    ("Reorder point & EOQ", "1.0", "services/replenishment_service.py", "ROP = d̄·L + SS ; EOQ = √(2DS/H) ; practical = round up to MOQ / multiple", "Per policy scope"),
    ("Replenishment policies (12)", "1.0", "services/replenishment_service.py", "Min-Max, ROP, fixed, periodic, order-up-to, base stock, L4L, Kanban, JIT, JIS, MRP, DRP", "Per policy scope"),
    ("Lead-time distribution", "1.0", "services/lead_time_service.py", "Empirical percentiles + lognormal fit (KS-tested); static vs observed basis is configurable", "Settings: engine.lt_basis"),
    ("Stock-out probability", "1.0", "services/projection_service.py", "P(stock-out) = Φ(−z); Supply(T) with ETA uncertainty vs Demand ~ N(μ, σ²T); E[shortage] = σ·G(z)", "Engine-wide"),
    ("Time-phased projection", "1.0", "services/projection_service.py", "Opening + receipts − order demand − unallocated forecast − BOM consumption − transfers; forecast consumption avoids double counting", "Engine-wide"),
    ("ICT Inventory Health Index", "1.0", "services/health_service.py", "Weighted mean of 9 linearly scaled components (ICT-defined, not an industry standard)", "Settings: weights.health"),
    ("Planner Priority Score", "1.0", "services/alert_service.py", "Weighted financial · service · production · customer · severity · time-to-impact", "Settings: weights.priority"),
    ("Supplier risk score", "1.0", "services/risk_service.py", "Weighted OTIF, lead-time reliability, quality, concentration, geo risk, expedite frequency", "Settings: weights.supplier_risk"),
    ("Root-cause clustering", "1.0", "services/incident_service.py", "Union-find over shared causal dimensions (shipment, PO, port, lane, supplier, production order) within a 14-day impact window", "Code constant WINDOW_DAYS"),
    ("Replenishment MILP", "1.0", "optimization/replenishment.py", "HiGHS MILP with MOQ, multiples, budget, capacity, trucks, shelf life, service floor; infeasibility diagnosis", "Settings: optimization.*"),
    ("Transfer / rebalancing LP", "1.0", "optimization/transfer.py", "Transportation LP with lane blocking reasons", "Settings: transfer.policy"),
    ("Allocation LP + policies", "1.0", "optimization/allocation.py, services/allocation_service.py", "Composite priority scoring; LP with minimum-fill floors", "Policy hierarchy"),
    ("Digital-twin simulation", "1.0", "services/simulation_service.py", "Monte-Carlo, common random numbers, 1d/1w/1m steps", "Per scenario"),
    ("Confidence score", "1.0", "services/engine.py", "Completeness, freshness, stability, model performance, constraint completeness", "Settings: weights.confidence"),
]


@bp.route("/governance")
def home():
    snap = H.snap() if H.has_data() else None
    elig = ex.eligibility(snap) if snap else []
    rules = Rule.query.order_by(Rule.code).all()
    kpis = KpiDefinition.query.order_by(KpiDefinition.category, KpiDefinition.code).all()
    changes = AuditLog.query.filter_by(category="CONFIG").order_by(AuditLog.id.desc()).limit(60).all()
    formulas = [{"name": v["name"], "formula": v["formula"], "use": v["use"]} for v in safety_stock_service.METHODS.values()] + \
               [{"name": f"Policy · {v['name']}", "formula": v["rule"], "use": ""} for v in replenishment_service.POLICIES.values()]
    return render_template("governance/home.html", registry=[dict(zip(["name", "version", "module", "description", "config"], m)) for m in MODEL_REGISTRY], elig=elig,
                           rules=[{"code": r.code, "name": r.name, "type": r.alert_type, "scope": r.scope, "cond": r.condition, "enabled": "Yes" if r.enabled else "No", "version": r.version, "ind": ", ".join(r.industries) or "all",
                                   "desc": r.description} for r in rules], kpis=[{"code": k.code, "name": k.name, "cat": k.category, "formula": k.formula, "win": k.window_days, "w": k.watch, "a": k.attention, "c": k.critical,
                                                                                   "en": "Yes" if k.enabled else "No"} for k in kpis],
                           changes=[{"when": c.timestamp, "actor": c.actor, "entity": f"{c.entity_type} {c.entity_id}", "event": c.event, "details": c.details} for c in changes], formulas=formulas,
                           learning=ex.learning_stats(), health_formula=health_service.FORMULA_TEXT)


@bp.route("/experiments", methods=["GET", "POST"])
def experiments():
    if request.method == "POST":
        from ..utils.security import has_perm
        if not has_perm("run_scenarios"):
            H.err("Your role cannot run experiments.")
        else:
            try:
                e = ex.run_experiment(request.form.get("name") or "Experiment", request.form.get("type", "service_level"), float(request.form["a"]), float(request.form["b"]), scope=request.form.get("scope") or None,
                                      hypothesis=request.form.get("hypothesis") or "", runs=min(int(request.form.get("runs") or 30), 100), created_by=H.actor())
                db.session.commit()
                H.ok(f"{e.exp_no} complete: arm {e.results['winner']} wins on {e.results['objective']}.")
            except (ValueError, KeyError) as err:
                db.session.rollback()
                H.err(str(err))
    exps = Experiment.query.order_by(Experiment.id.desc()).limit(30).all()
    return render_template("governance/experiments.html", exps=exps, types=ex.EXPERIMENT_TYPES, learning=ex.learning_stats())


@bp.route("/audit")
def audit():
    cat, ent, q = request.args.get("category"), request.args.get("entity"), (request.args.get("q") or "").strip()
    qs = AuditLog.query
    if cat:
        qs = qs.filter_by(category=cat)
    if ent:
        qs = qs.filter_by(entity_type=ent)
    if q:
        qs = qs.filter(AuditLog.entity_id.ilike(f"%{q}%") | AuditLog.event.ilike(f"%{q}%") | AuditLog.actor.ilike(f"%{q}%"))
    rows = qs.order_by(AuditLog.id.desc()).limit(500).all()
    cats = [c[0] for c in db.session.query(AuditLog.category).distinct().all()]
    ents = [c[0] for c in db.session.query(AuditLog.entity_type).distinct().all()]
    return render_template("governance/audit.html", rows=rows, cats=cats, ents=ents, sel={"category": cat, "entity": ent, "q": q})
