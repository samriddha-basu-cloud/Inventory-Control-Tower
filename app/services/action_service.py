"""Closed-loop action workflow.

    DETECT → ANALYZE → GENERATE OPTIONS → SIMULATE → CHECK POLICY → (AUTONOMY / APPROVAL) → EXECUTE → VERIFY → AUDIT

ICT never blindly executes. Actions start in RECOMMENDATION / SIMULATION_ONLY mode; the execution connector is the
MockExecutionConnector, whose results are always labelled MOCK. Autonomy levels:
    0 Observe only · 1 Recommend · 2 Auto-prepare transaction · 3 Auto-execute within guardrails · 4 Closed-loop
Rules: NEVER_AUTO beats everything (deny wins); REQUIRE_APPROVAL caps at level 2; ALLOW_AUTO grants its level when its
guardrails (max cost, min confidence, criticality...) hold. Everything is configurable data (AutonomyRule rows).
"""
from __future__ import annotations

import copy
import math
from datetime import timedelta

from ..connectors.execution import CONNECTORS
from ..extensions import db
from ..models import Action, Approval, AutonomyRule, Execution, Recommendation, Alert
from ..utils.security import ROLE_RANK
from . import allocation_service, audit_service as audit, carbon_service as carbon, ledger_service as ledger
from . import optimization_service as opt
from . import settings_service as S
from .engine import compute_pair
from .snapshot import get_snapshot

ACTION_TYPES = {
    "CREATE_PO": "Create PO", "EXPEDITE_PO": "Expedite PO", "TRANSFER_STOCK": "Transfer stock", "CHANGE_QUANTITY": "Change quantity", "CHANGE_DATE": "Change date",
    "ALLOCATE_STOCK": "Allocate stock", "DEALLOCATE_STOCK": "Deallocate stock", "REPLENISH": "Replenish", "RELEASE_STOCK": "Release stock", "QUARANTINE": "Quarantine",
    "REVIEW_EXPIRY": "Review expiry", "REVIEW_OBSOLESCENCE": "Review obsolescence", "CHANGE_SAFETY_STOCK": "Change safety stock", "CHANGE_REORDER_POINT": "Change reorder point",
}
PURCHASE_LIKE = {"CREATE_PO", "EXPEDITE_PO", "REPLENISH"}
STATUSES = ["PROPOSED", "PENDING_APPROVAL", "ESCALATED", "APPROVED", "EXECUTED", "VERIFIED", "REJECTED", "FAILED", "OBSERVED", "CANCELLED"]
LEVEL_NAMES = {0: "Observe only", 1: "Recommend", 2: "Auto-prepare transaction", 3: "Auto-execute within guardrails", 4: "Closed-loop execution"}
ROLE_FOR_RANK = {1: "Inventory Planner", 2: "Supply Chain Manager", 3: "Executive", 4: "Administrator"}


class ActionError(ValueError):
    pass


# ---------------------------------------------------------------------------------------------------------- simulate
def simulate_action(a: Action) -> dict:
    """CURRENT vs EXPECTED state for the action, computed by re-running the engine on a copy (no data is changed)."""
    snap = get_snapshot()
    key = (a.item_id, a.location_id)
    inp, r = snap.inputs.get(key), snap.results.get(key)
    if not inp:
        return {"error": "Unknown item-location; nothing to simulate."}
    cfg = snap.cfg
    p = a.payload or {}
    i2 = copy.deepcopy(inp)
    cost, co2, wc_delta = a.cost or 0.0, 0.0, 0.0
    arrival = p.get("arrival_days")
    qty = a.qty or 0.0
    wkg = float(inp.item.get("weight_kg") or 1.0)
    t = a.action_type
    if t in PURCHASE_LIKE and t != "EXPEDITE_PO":
        arr = arrival if arrival is not None else max(1, math.ceil(r.lt_plan))
        i2.inbound.append({"kind": "PO", "ref": "PROPOSED", "qty": qty, "in_transit": False, "eta_day": arr, "promised_day": arr, "supplier_id": inp.supplier_id, "lane": None,
                           "mode": inp.mode, "origin": None, "delay_days": 0})
        cost = cost or (cfg.ordering_cost + carbon.freight_cost(wkg * qty, inp.distance_km, inp.mode))
        co2 = carbon.emissions_kg(wkg * qty, inp.distance_km, inp.mode)
        wc_delta = qty * r.unit_cost
    elif t == "EXPEDITE_PO":
        new_eta = arrival if arrival is not None else 2
        for ib in i2.inbound:
            if ib["ref"] == p.get("ref") or (not p.get("ref") and ib["kind"] == "PO"):
                ib["eta_day"], ib["promised_day"] = new_eta, new_eta
        cmp_ = carbon.compare_modes(wkg * qty, inp.distance_km, inp.mode)
        cost = cost or max(cmp_[1]["cost"] - cmp_[0]["cost"], 0.0)
        co2 = max(cmp_[1]["co2e_kg"] - cmp_[0]["co2e_kg"], 0.0)
    elif t == "TRANSFER_STOCK":
        days = arrival if arrival is not None else 3
        i2.inbound.append({"kind": "TO", "ref": "PROPOSED", "qty": qty, "in_transit": False, "eta_day": days, "promised_day": days, "supplier_id": None, "lane": None,
                           "mode": "ROAD", "origin": None, "delay_days": 0})
        cost = cost or carbon.freight_cost(wkg * qty, p.get("distance_km", inp.distance_km), "ROAD")
        co2 = carbon.emissions_kg(wkg * qty, p.get("distance_km", inp.distance_km), "ROAD")
    elif t == "CHANGE_SAFETY_STOCK":
        i2.policy = {**i2.policy, **(p.get("params") or {})}
    elif t == "CHANGE_REORDER_POINT":
        i2.repl = {**i2.repl, **(p.get("params") or {})}
    elif t == "ALLOCATE_STOCK":
        i2.committed += qty
    elif t == "DEALLOCATE_STOCK":
        i2.committed = max(0.0, i2.committed - qty)
    elif t == "QUARANTINE":
        i2.stock["UNRESTRICTED"] = i2.stock.get("UNRESTRICTED", 0.0) - qty
        i2.stock["QUARANTINED"] = i2.stock.get("QUARANTINED", 0.0) + qty
    elif t == "RELEASE_STOCK":
        i2.stock["QUARANTINED"] = i2.stock.get("QUARANTINED", 0.0) - qty
        i2.stock["UNRESTRICTED"] = i2.stock.get("UNRESTRICTED", 0.0) + qty
    r2 = compute_pair(i2, cfg, with_projection=False)

    def snapshot_of(x, extra_cost=0.0):
        return {"usable_on_hand": x.pos["usable_on_hand"], "position": x.pos["position"], "available": x.pos["available"], "inventory_value": x.on_hand_value,
                "stockout_prob": x.stockout_prob, "risk_level": x.risk_level, "service_impact": x.service_impact, "expected_shortage": x.exp_short,
                "lost_margin": x.lost_margin, "safety_stock": x.ss, "rop": x.rop, "days_supply": x.days_supply,
                "working_capital": x.on_hand_value + x.in_transit_value + x.on_order_value}

    cur, exp = snapshot_of(r), snapshot_of(r2)
    exp["working_capital"] = exp["working_capital"] + (wc_delta if t in PURCHASE_LIKE and t != "EXPEDITE_PO" else 0.0)
    exp["cost"] = cost
    exp["carbon_kg"] = co2
    cur["cost"], cur["carbon_kg"] = 0.0, 0.0
    delta = {k: exp[k] - cur[k] for k in ("usable_on_hand", "position", "inventory_value", "stockout_prob", "service_impact", "expected_shortage", "working_capital", "cost", "carbon_kg")}
    return {"current": cur, "expected": exp, "delta": delta, "cost": cost, "carbon_kg": co2,
            "note": "Computed by the same engine on a copy of the data - no production data was modified."}


# ---------------------------------------------------------------------------------------------------------- policy
def check_policy(a: Action) -> dict:
    snap = get_snapshot()
    key = (a.item_id, a.location_id)
    inp, r = snap.inputs.get(key), snap.results.get(key)
    checks = []

    def chk(name, ok, detail, blocking=True):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "blocking": blocking and not ok})

    if not inp:
        chk("Item-location exists", False, "Unknown item/location")
        return {"checks": checks, "blocking": True}
    t, qty = a.action_type, a.qty or 0.0
    if t in PURCHASE_LIKE | {"TRANSFER_STOCK", "ALLOCATE_STOCK", "QUARANTINE", "RELEASE_STOCK", "CHANGE_QUANTITY"}:
        chk("Quantity is positive", qty > 0, f"quantity {qty:,.0f}")
    if t in ("CREATE_PO", "REPLENISH"):
        moq, mult = inp.moq, inp.multiple
        chk("Meets MOQ", (not moq) or qty >= moq - 1e-9, f"MOQ {moq or 0:,.0f}")
        chk("Order multiple", (not mult) or abs(qty / mult - round(qty / mult)) < 1e-6, f"multiple {mult or 1:,.0f}")
        chk("Item lifecycle allows purchasing", inp.item.get("lifecycle_status") not in ("OBSOLETE",), f"lifecycle {inp.item.get('lifecycle_status')}")
        budget = S.get("optimization.budget")
        chk("Within budget", (not budget) or qty * r.unit_cost <= budget, f"value {qty * r.unit_cost:,.0f} vs budget {budget or 0:,.0f}")
        if inp.item.get("shelf_life_days"):
            chk("Shelf-life vs demand", qty <= r.d_mean * inp.item["shelf_life_days"] * 0.8 + 1, "quantity should be consumable within 80 % of shelf life", blocking=False)
    if t == "TRANSFER_STOCK":
        src = (a.payload or {}).get("from_location_id")
        sk = (a.item_id, src)
        if sk in snap.results:
            dr = snap.results[sk]
            tpol = S.get("transfer.policy") or {"keep_safety_stock_at_donor": True}
            floor = dr.ss if tpol["keep_safety_stock_at_donor"] else 0.0
            chk("Donor keeps safety stock", dr.pos["available"] - qty >= floor - 1e-9, f"donor available {dr.pos['available']:,.0f} − {qty:,.0f} ≥ SS {floor:,.0f}")
            chk("Donor has the stock", dr.pos["usable_on_hand"] >= qty, f"usable {dr.pos['usable_on_hand']:,.0f}")
        cap = inp.loc.get("capacity_units")
        if cap:
            oh = sum(snap.results[k].pos["on_hand"] for k in snap.keys_for_loc(a.location_id))
            chk("Destination capacity", oh + qty <= cap * 1.0, f"utilisation after {(oh + qty) / cap:.0%}")
    if t in ("ALLOCATE_STOCK",):
        chk("Not over-allocating", qty <= r.pos["available"] + 1e-9, f"available {r.pos['available']:,.0f}")
    if t in ("QUARANTINE",):
        chk("Stock available to quarantine", qty <= r.pos["usable_on_hand"] + 1e-9, f"usable {r.pos['usable_on_hand']:,.0f}")
    if t == "RELEASE_STOCK":
        chk("Stock is in quarantine", qty <= r.pos["on_hand"] - r.pos["usable_on_hand"] + 1e-9, "quarantined/blocked stock")
        chk("Regulated release needs a human", True, "Release is never auto-executed for regulated industries", blocking=False)
    if t == "EXPEDITE_PO":
        chk("A faster freight mode exists", S.get("freight.expedite_mode").get(inp.mode, inp.mode) != inp.mode, f"mode {inp.mode}")
    return {"checks": checks, "blocking": any(c["blocking"] for c in checks)}


# ---------------------------------------------------------------------------------------------------------- autonomy
def _matches(rule: AutonomyRule, a: Action, inp, confidence: float, cost: float) -> bool:
    if rule.action_types and a.action_type not in rule.action_types:
        return False
    c = rule.conditions or {}
    crit = (inp.item.get("criticality") if inp else None)
    if "max_cost" in c and cost > c["max_cost"]:
        return False
    if "min_cost" in c and cost < c["min_cost"]:
        return False
    if "min_confidence" in c and (confidence or 0) < c["min_confidence"]:
        return False
    if "criticality_in" in c and crit not in c["criticality_in"]:
        return False
    if "criticality_not_in" in c and crit in c["criticality_not_in"]:
        return False
    if "industry" in c and (inp.item.get("industry") if inp else None) != c["industry"]:
        return False
    return True


def decide_autonomy(a: Action, policy: dict | None = None) -> dict:
    snap = get_snapshot()
    inp = snap.inputs.get((a.item_id, a.location_id))
    cost = max(a.cost or 0.0, (a.qty or 0.0) * (inp.item.get("unit_cost") or 0.0) if a.action_type in PURCHASE_LIKE else (a.cost or 0.0))
    base = int(S.get("autonomy.default_level"))
    level, matched, reasons = base, [], []
    never = False
    cap = None
    rules = AutonomyRule.query.filter_by(enabled=True).order_by(AutonomyRule.priority).all()
    for rule in rules:
        if not _matches(rule, a, inp, a.confidence or 0.0, cost):
            continue
        matched.append({"rule": rule.name, "effect": rule.effect, "level": rule.level})
        if rule.effect == "NEVER_AUTO":
            never = True
            reasons.append(f"Blocked from auto-execution: {rule.name}")
        elif rule.effect == "REQUIRE_APPROVAL":
            cap = 2 if cap is None else min(cap, 2)
            reasons.append(f"Approval required: {rule.name}")
        elif rule.effect == "ALLOW_AUTO":
            level = max(level, rule.level)
            reasons.append(f"Guardrail rule satisfied: {rule.name}")
    if never:
        level = min(level, 1)
    elif cap is not None:
        level = min(level, cap)
    if policy and policy.get("blocking"):
        level = min(level, 1)
        reasons.append("Policy check has blocking violations - human review required")
    # approver role from the cost ladder
    role = None
    for band in S.get("approval.matrix"):
        if band["max_cost"] is None or cost <= band["max_cost"]:
            role = band["role"]
            break
    if a.action_type in ("RELEASE_STOCK", "QUARANTINE"):
        role = "Supply Chain Manager" if ROLE_RANK.get(role or "", 1) < 2 else role
    decision = {0: "OBSERVE", 1: "APPROVAL", 2: "PREPARE_THEN_APPROVAL", 3: "AUTO_EXECUTE", 4: "CLOSED_LOOP"}[level]
    return {"level": level, "level_name": LEVEL_NAMES[level], "decision": decision, "auto": level >= 3, "matched_rules": matched, "reasons": reasons,
            "required_role": role, "cost_considered": cost, "default_level": base}


# ---------------------------------------------------------------------------------------------------------- lifecycle
def create_action(action_type: str, *, item_id: int, location_id: int, qty: float = 0.0, supplier_id: int | None = None, to_location_id: int | None = None,
                  need_date=None, cost: float = 0.0, why: str = "", payload: dict | None = None, alternatives: list | None = None, confidence: float | None = None,
                  risk_level: str | None = None, alert_id: int | None = None, recommendation_id: int | None = None, created_by: str = "system",
                  auto_process: bool = True) -> Action:
    if action_type not in ACTION_TYPES:
        raise ActionError(f"Unknown action type '{action_type}'")
    snap = get_snapshot()
    if (item_id, location_id) not in snap.results:
        raise ActionError("Missing SKU / location: no such item-location in the network.")
    if action_type in PURCHASE_LIKE | {"TRANSFER_STOCK", "ALLOCATE_STOCK"} and (qty or 0) <= 0:
        raise ActionError("Quantity must be positive.")
    r = snap.results[(item_id, location_id)]
    a = Action(action_type=action_type, item_id=item_id, location_id=location_id, to_location_id=to_location_id, supplier_id=supplier_id or snap.inputs[(item_id, location_id)].supplier_id,
               qty=qty, need_date=need_date, cost=cost, why=why, payload=payload or {}, alternatives=alternatives or [], confidence=confidence if confidence is not None else r.confidence,
               risk_level=risk_level or r.risk_level, alert_id=alert_id, recommendation_id=recommendation_id, created_by=created_by, status="PROPOSED",
               mode=S.get("execution.mode"))
    db.session.add(a)
    db.session.flush()
    a.action_no = f"ACT-{a.id:06d}"
    audit.log("ACTION", "Action", a.action_no, "action_created", {"type": action_type, "qty": qty, "item_id": item_id, "location_id": location_id, "cost": cost}, actor=created_by)
    if auto_process:
        process(a)
    return a


def process(a: Action) -> Action:
    """SIMULATE → CHECK POLICY → AUTONOMY decision (may auto-execute)."""
    a.simulation = simulate_action(a)
    if a.simulation.get("cost") and not a.cost:
        a.cost = a.simulation["cost"]
    a.policy_check = check_policy(a)
    a.autonomy = decide_autonomy(a, a.policy_check)
    dec = a.autonomy
    a.required_role = dec["required_role"]
    a.approval_required = not dec["auto"]
    a.approval_reason = "; ".join(dec["reasons"]) or f"Autonomy level {dec['level']} ({dec['level_name']})"
    if dec["level"] == 0:
        a.status = "OBSERVED"
    elif dec["auto"] and not a.policy_check["blocking"]:
        a.status = "APPROVED"
        db.session.add(Approval(action_id=a.id, approver="AUTONOMY-POLICY", role="system", decision="APPROVE",
                                comment=f"Auto-approved by autonomy level {dec['level']}: " + "; ".join(dec["reasons"])))
        audit.log("APPROVAL", "Action", a.action_no, "auto_approved", dec)
        execute(a, actor="AUTONOMY-POLICY")
    else:
        a.status = "PENDING_APPROVAL"
    audit.log("ACTION", "Action", a.action_no, "action_processed", {"status": a.status, "autonomy": dec["decision"], "policy_blocking": a.policy_check["blocking"]},
              trace={"simulation": a.simulation, "policy": a.policy_check, "autonomy": dec})
    return a


def _authorise(a: Action, actor: str, role: str, decision: str):
    need = a.required_role or "Inventory Planner"
    if role != "Administrator" and ROLE_RANK.get(role, 0) < ROLE_RANK.get(need, 1):
        raise ActionError(f"Role '{role}' cannot {decision.lower()} this action: it requires {need} or higher (cost {a.cost or 0:,.0f}).")
    if S.get("approval.no_self_approval") and actor == a.created_by and role != "Administrator" and (a.cost or 0) > 0:
        raise ActionError("Segregation of duties: the creator cannot approve their own action.")


def approve(action_id: int, actor: str, role: str, comment: str = "", modified: dict | None = None) -> Action:
    a = _get(action_id)
    if a.status not in ("PENDING_APPROVAL", "ESCALATED", "PROPOSED"):
        raise ActionError(f"Action {a.action_no} is {a.status}; it cannot be approved.")
    _authorise(a, actor, role, "APPROVE")
    if a.policy_check.get("blocking"):
        raise ActionError("Blocked by policy: " + "; ".join(c["detail"] for c in a.policy_check["checks"] if c["blocking"]) + ". Modify the action first.")
    db.session.add(Approval(action_id=a.id, approver=actor, role=role, decision="APPROVE", comment=comment, modified_payload=modified))
    a.status = "APPROVED"
    audit.log("APPROVAL", "Action", a.action_no, "approved", {"by": actor, "role": role, "comment": comment}, actor=actor)
    if S.get("execution.mode") in ("SIMULATION_ONLY", "LIVE"):
        execute(a, actor=actor)
    return a


def reject(action_id: int, actor: str, role: str, comment: str = "") -> Action:
    a = _get(action_id)
    if a.status in ("EXECUTED", "VERIFIED", "REJECTED", "CANCELLED"):
        raise ActionError(f"Action {a.action_no} is already {a.status}.")
    db.session.add(Approval(action_id=a.id, approver=actor, role=role, decision="REJECT", comment=comment))
    a.status = "REJECTED"
    if a.recommendation_id:
        rec = Recommendation.query.get(a.recommendation_id)
        if rec:
            rec.status = "DISMISSED"
    audit.log("APPROVAL", "Action", a.action_no, "rejected", {"by": actor, "comment": comment}, actor=actor)
    return a


def escalate(action_id: int, actor: str, role: str, comment: str = "") -> Action:
    a = _get(action_id)
    cur = ROLE_RANK.get(a.required_role or "Inventory Planner", 1)
    a.required_role = ROLE_FOR_RANK.get(min(cur + 1, 3), "Executive")
    a.status = "ESCALATED"
    db.session.add(Approval(action_id=a.id, approver=actor, role=role, decision="ESCALATE", comment=comment))
    audit.log("APPROVAL", "Action", a.action_no, "escalated", {"to_role": a.required_role, "comment": comment}, actor=actor)
    return a


def modify(action_id: int, actor: str, role: str, *, qty: float | None = None, need_date=None, comment: str = "", payload_update: dict | None = None) -> Action:
    a = _get(action_id)
    if a.status in ("EXECUTED", "VERIFIED", "REJECTED", "CANCELLED"):
        raise ActionError(f"Action {a.action_no} is {a.status} and cannot be modified.")
    before = {"qty": a.qty, "need_date": str(a.need_date) if a.need_date else None}
    if qty is not None:
        if qty <= 0:
            raise ActionError("Quantity must be positive.")
        a.qty = qty
        a.cost = 0.0
    if need_date is not None:
        a.need_date = need_date
    a.payload = {**(a.payload or {}), **(payload_update or {})}
    db.session.add(Approval(action_id=a.id, approver=actor, role=role, decision="MODIFY", comment=comment, modified_payload={"before": before, "qty": qty}))
    audit.log("APPROVAL", "Action", a.action_no, "modified", {"by": actor, "before": before, "qty": qty}, actor=actor)
    process(a)
    if a.status == "APPROVED":     # modification re-opens the approval unless autonomy re-approves it
        pass
    return a


def execute(a: Action, actor: str = "system") -> Execution | None:
    mode = S.get("execution.mode")
    if a.status not in ("APPROVED",):
        raise ActionError(f"Action {a.action_no} is {a.status}: only APPROVED actions can execute.")
    if mode == "RECOMMENDATION":
        audit.log("EXEC", "Action", a.action_no, "execution_skipped", {"reason": "execution.mode=RECOMMENDATION"}, actor=actor)
        return None
    if mode == "LIVE":
        a.status = "FAILED"
        ex = Execution(action_id=a.id, connector="none", is_mock=False, mode=mode, request=a.payload, response={"error": "No live connector is configured."}, status="FAILED")
        db.session.add(ex)
        audit.log("EXEC", "Action", a.action_no, "execution_failed", {"reason": "LIVE mode requires a configured connector; none is available"}, actor=actor)
        raise ActionError("Execution mode is LIVE but no live connector is configured. Configure a real ERP/WMS connector or switch to SIMULATION_ONLY.")
    conn = CONNECTORS["mock"]
    payload = {"action_no": a.action_no, "type": a.action_type, "sku": a.item.sku, "location": a.location.code, "qty": a.qty, "payload": a.payload}
    res = conn.execute(a.action_type, payload, dry_run=True)
    applied = {}
    if res.ok and S.get("execution.apply_to_local_data"):
        applied = conn.apply_locally(a, res.external_ref or "")
        res.response["applied_locally"] = bool(applied)
        res.response["applied"] = applied
    verify = conn.verify(res)
    ex = Execution(action_id=a.id, connector=conn.name, is_mock=True, mode=mode, request=payload, response=res.response, status="SIMULATED" if res.ok else "FAILED",
                   verified=bool(verify["verified"]), verification=verify, external_ref=res.external_ref)
    db.session.add(ex)
    a.status = "VERIFIED" if verify["verified"] else ("EXECUTED" if res.ok else "FAILED")
    if a.recommendation_id:
        rec = Recommendation.query.get(a.recommendation_id)
        if rec:
            rec.status = "ACTIONED"
    if a.alert_id:
        al = Alert.query.get(a.alert_id)
        if al and al.status in ("New", "Acknowledged", "Investigating", "Action Proposed", "Approved"):
            al.status = "Executed"
    audit.log("EXEC", "Action", a.action_no, "executed_mock", {"connector": conn.name, "ref": res.external_ref, "verified": verify["verified"], "mode": mode, "applied_locally": bool(applied)},
              trace={"request": payload, "response": res.response, "verification": verify}, actor=actor)
    if applied:
        S.bump_version()
    return ex


def _get(action_id: int) -> Action:
    a = Action.query.get(action_id)
    if not a:
        raise ActionError("Action not found.")
    return a


def from_recommendation(rec_id: int, actor: str, option_index: int | None = None, qty_override: float | None = None) -> Action:
    rec = Recommendation.query.get(rec_id)
    if not rec:
        raise ActionError("Recommendation not found.")
    pl = rec.payload or {}
    opts = pl.get("options") or []
    opt_ = (opts[option_index] if option_index is not None and 0 <= option_index < len(opts) else pl.get("recommended"))
    key = (rec.item_id, rec.location_id)
    snap = get_snapshot()
    supplier_id = snap.inputs[key].supplier_id if key in snap.inputs else None
    if opt_ and opt_.get("type") != "DO_NOTHING":
        typ = opt_["type"]
        qty = qty_override or opt_.get("qty") or 0.0
        payload = {"arrival_days": opt_.get("arrival_days"), "ref": opt_.get("ref"), "from_location_id": opt_.get("from_location_id"), "option": opt_.get("label"),
                   "alt_sku": opt_.get("alt_sku")}
        why = rec.summary
        return create_action(typ, item_id=rec.item_id, location_id=rec.location_id, qty=qty, supplier_id=opt_.get("supplier_id") or supplier_id, cost=opt_.get("incremental_cost", 0.0),
                             why=why, payload=payload, alternatives=[{k: o.get(k) for k in ("type", "label", "qty", "incremental_cost", "arrival_days", "feasible", "violations")} for o in opts],
                             confidence=rec.confidence, alert_id=rec.alert_id, recommendation_id=rec.id, created_by=actor)
    if rec.kind == "TRANSFER" and pl.get("transfer"):
        t = pl["transfer"]
        return create_action("TRANSFER_STOCK", item_id=t["item_id"], location_id=t["to_id"], to_location_id=t["to_id"], qty=qty_override or t["qty"], cost=t["freight_cost"], why=rec.summary,
                             payload={"from_location_id": t["from_id"], "arrival_days": t["eta_days"], "distance_km": t["distance_km"]}, confidence=rec.confidence,
                             recommendation_id=rec.id, created_by=actor)
    typ = {"REVIEW_EXPIRY": "REVIEW_EXPIRY", "REVIEW_OBSOLESCENCE": "REVIEW_OBSOLESCENCE"}.get(rec.kind, "REVIEW_OBSOLESCENCE")
    return create_action(typ, item_id=rec.item_id, location_id=rec.location_id, qty=0.0, why=rec.summary, payload=pl, confidence=rec.confidence, alert_id=rec.alert_id,
                         recommendation_id=rec.id, created_by=actor)
