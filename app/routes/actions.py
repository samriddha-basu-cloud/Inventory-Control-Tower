"""Action Center, recommendations, approvals and the Autonomy Center."""
from __future__ import annotations

import json

from flask import Blueprint, redirect, render_template, request, url_for

from ..extensions import db
from ..models import Action, AutonomyRule, Recommendation
from ..services import action_service as A, audit_service, settings_service as S
from ..utils.security import ROLE_RANK, require
from . import helpers as H

bp = Blueprint("actions", __name__)


@bp.route("/actions")
def center():
    status = request.args.get("status")
    q = Action.query
    if status:
        q = q.filter(Action.status == status)
    acts = q.order_by(Action.id.desc()).limit(300).all()
    rows = [{"id": a.id, "no": a.action_no, "type": A.ACTION_TYPES.get(a.action_type, a.action_type), "sku": a.item.sku if a.item else "", "node": a.location.code if a.location else "", "qty": a.qty, "cost": a.cost,
             "risk": a.risk_level, "conf": a.confidence, "status": a.status, "need": a.required_role or "", "mode": a.mode, "level": (a.autonomy or {}).get("level_name", ""), "by": a.created_by, "at": a.created_at,
             "why": (a.why or "")[:110]} for a in acts]
    recs = Recommendation.query.filter_by(status="OPEN").order_by(Recommendation.priority_score.desc()).limit(80).all()
    rrows = [{"id": r.id, "no": r.rec_no, "kind": r.kind, "title": r.title, "sku": r.item.sku if r.item else "", "node": r.location.code if r.location else "", "pri": r.priority_score, "conf": r.confidence,
              "dq": r.data_quality, "cost": r.cost, "summary": r.summary} for r in recs]
    counts = {s: Action.query.filter_by(status=s).count() for s in A.STATUSES}
    pending_mine = [a for a in acts if a.status in ("PENDING_APPROVAL", "ESCALATED") and (ROLE_RANK.get(H.role(), 0) >= ROLE_RANK.get(a.required_role or "Inventory Planner", 1) or H.role() == "Administrator")]
    return render_template("actions/center.html", rows=rows, rrows=rrows, counts=counts, status=status, statuses=A.STATUSES, mine=len(pending_mine), mode=S.get("execution.mode"), types=A.ACTION_TYPES)


@bp.route("/recommendations/<int:rec_id>")
def recommendation(rec_id):
    r = Recommendation.query.get_or_404(rec_id)
    return render_template("actions/recommendation.html", r=r, pl=r.payload or {})


@bp.route("/recommendations/<int:rec_id>/act", methods=["POST"])
@require("create_action")
def recommendation_act(rec_id):
    try:
        idx = request.form.get("option")
        a = A.from_recommendation(rec_id, H.actor(), int(idx) if idx not in (None, "") else None, H.fnum(request.form.get("qty")))
        db.session.commit()
        H.ok(f"Action {a.action_no} created: {a.status.replace('_', ' ').title()}.")
        return redirect(url_for("actions.action_detail", action_id=a.id))
    except A.ActionError as e:
        db.session.rollback()
        H.err(str(e))
        return redirect(url_for("actions.recommendation", rec_id=rec_id))


@bp.route("/recommendations/<int:rec_id>/dismiss", methods=["POST"])
@require("create_action")
def recommendation_dismiss(rec_id):
    r = Recommendation.query.get_or_404(rec_id)
    r.status = "DISMISSED"
    audit_service.log("ACTION", "Recommendation", r.rec_no, "dismissed", {"reason": request.form.get("reason")}, actor=H.actor())
    db.session.commit()
    H.ok(f"{r.rec_no} dismissed.")
    return redirect(url_for("actions.center"))


@bp.route("/actions/<int:action_id>")
def action_detail(action_id):
    a = Action.query.get_or_404(action_id)
    can = H.role() == "Administrator" or ROLE_RANK.get(H.role(), 0) >= ROLE_RANK.get(a.required_role or "Inventory Planner", 1)
    flow = ["DETECT", "ANALYZE", "GENERATE OPTIONS", "SIMULATE", "CHECK POLICY", "APPROVAL", "EXECUTE", "VERIFY", "AUDIT"]
    done = {"PROPOSED": 5, "PENDING_APPROVAL": 5, "ESCALATED": 5, "OBSERVED": 3, "APPROVED": 6, "EXECUTED": 7, "VERIFIED": 8, "REJECTED": 5, "FAILED": 6, "CANCELLED": 5}.get(a.status, 5)
    return render_template("actions/detail.html", a=a, can=can, flow=flow, done=done, mode=S.get("execution.mode"))


def _do(fn, action_id, **kw):
    try:
        fn(action_id, H.actor(), H.role(), **kw)
        db.session.commit()
        H.ok("Done. Recorded in the audit trail.")
    except A.ActionError as e:
        db.session.rollback()
        H.err(str(e))
    return redirect(url_for("actions.action_detail", action_id=action_id))


@bp.route("/actions/<int:action_id>/approve", methods=["POST"])
@require("approve")
def approve(action_id):
    return _do(A.approve, action_id, comment=request.form.get("comment", ""))


@bp.route("/actions/<int:action_id>/reject", methods=["POST"])
@require("approve")
def reject(action_id):
    return _do(A.reject, action_id, comment=request.form.get("comment", ""))


@bp.route("/actions/<int:action_id>/escalate", methods=["POST"])
@require("approve")
def escalate(action_id):
    return _do(A.escalate, action_id, comment=request.form.get("comment", ""))


@bp.route("/actions/<int:action_id>/modify", methods=["POST"])
@require("approve")
def modify(action_id):
    return _do(A.modify, action_id, qty=H.fnum(request.form.get("qty")), need_date=H.fdate(request.form.get("need_date")), comment=request.form.get("comment", ""))


@bp.route("/actions/<int:action_id>/execute", methods=["POST"])
@require("execute")
def execute(action_id):
    a = Action.query.get_or_404(action_id)
    try:
        A.execute(a, actor=H.actor())
        db.session.commit()
        H.ok("Execution attempted through the mock connector (simulation; no real system was changed).")
    except A.ActionError as e:
        db.session.commit()
        H.err(str(e))
    return redirect(url_for("actions.action_detail", action_id=action_id))


# ------------------------------------------------------------------------------------------------ autonomy
@bp.route("/autonomy")
def autonomy():
    rules = AutonomyRule.query.order_by(AutonomyRule.priority).all()
    demo = None
    if request.args.get("t"):
        class _A:  # transient action for the what-if decision
            pass
        from ..models import Item
        a = Action(action_type=request.args["t"], item_id=None, location_id=None, qty=H.fnum(request.args.get("qty"), 0), cost=H.fnum(request.args.get("cost"), 0), confidence=H.fnum(request.args.get("conf"), 0.8))
        snap = H.snap()
        sku, loc = request.args.get("sku"), request.args.get("loc")
        key = next((k for k, i in snap.inputs.items() if i.sku == sku and i.loc_code == loc), None)
        if key:
            a.item_id, a.location_id = key
        demo = A.decide_autonomy(a)
    return render_template("actions/autonomy.html", rules=rules, level=S.get("autonomy.default_level"), mode=S.get("execution.mode"), matrix=S.get("approval.matrix"), levels=A.LEVEL_NAMES, demo=demo,
                           types=A.ACTION_TYPES, apply_local=S.get("execution.apply_to_local_data"), q=request.args, no_self=S.get("approval.no_self_approval"))


@bp.route("/autonomy/settings", methods=["POST"])
@require("configure")
def autonomy_settings():
    lvl = int(request.form.get("level", 1))
    mode = request.form.get("mode", "SIMULATION_ONLY")
    if lvl not in range(5) or mode not in ("RECOMMENDATION", "SIMULATION_ONLY", "LIVE"):
        H.err("Invalid autonomy level or execution mode.")
        return redirect(url_for("actions.autonomy"))
    if mode == "LIVE":
        H.err("LIVE execution needs a configured real connector; none exists. ICT will not pretend otherwise. Use SIMULATION_ONLY.")
        return redirect(url_for("actions.autonomy"))
    S.set_value("autonomy.default_level", lvl, actor=H.actor())
    S.set_value("execution.mode", mode, actor=H.actor())
    S.set_value("execution.apply_to_local_data", bool(request.form.get("apply_local")), actor=H.actor())
    S.set_value("approval.no_self_approval", bool(request.form.get("no_self")), actor=H.actor())
    try:
        matrix = json.loads(request.form.get("matrix") or "[]")
        if matrix and all("role" in b and "max_cost" in b for b in matrix):
            S.set_value("approval.matrix", matrix, actor=H.actor())
    except json.JSONDecodeError:
        H.err("Approval matrix must be valid JSON; other settings were saved.")
    db.session.commit()
    H.ok("Autonomy settings saved.")
    return redirect(url_for("actions.autonomy"))


@bp.route("/autonomy/rule", methods=["POST"])
@require("configure")
def autonomy_rule():
    op = request.form.get("op")
    if op == "add":
        try:
            cond = json.loads(request.form.get("conditions") or "{}")
            types = [t.strip() for t in (request.form.get("types") or "").split(",") if t.strip()]
            bad = [t for t in types if t not in A.ACTION_TYPES]
            if bad:
                raise ValueError(f"unknown action types {bad}")
            if request.form.get("effect") not in ("ALLOW_AUTO", "REQUIRE_APPROVAL", "NEVER_AUTO"):
                raise ValueError("invalid effect")
            r = AutonomyRule(name=(request.form.get("name") or "Rule")[:160], level=int(request.form.get("level", 3)), action_types=types, effect=request.form["effect"], conditions=cond,
                             priority=int(request.form.get("priority", 100)), description=(request.form.get("description") or "")[:300])
            db.session.add(r)
            audit_service.log("CONFIG", "AutonomyRule", r.name, "rule_added", {"effect": r.effect, "conditions": cond, "types": types}, actor=H.actor())
            H.ok("Rule added.")
        except (ValueError, json.JSONDecodeError) as e:
            H.err(f"Rule not saved: {e}")
    else:
        r = AutonomyRule.query.get_or_404(int(request.form.get("id", 0)))
        if op == "toggle":
            r.enabled = not r.enabled
        elif op == "delete":
            db.session.delete(r)
        audit_service.log("CONFIG", "AutonomyRule", r.name, f"rule_{op}", {}, actor=H.actor())
    S.bump_version()
    db.session.commit()
    return redirect(url_for("actions.autonomy"))
