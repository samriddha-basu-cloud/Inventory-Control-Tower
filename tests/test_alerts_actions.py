"""Alert generation, deduplication, root-cause clustering, priority score, autonomy and the approval workflow."""
import pytest

from app.extensions import db
from app.models import Action, Alert, AuditLog, Execution, Incident, Rule
from app.services import action_service as A
from app.services import alert_service as al
from app.services import incident_service
from app.services import settings_service as S
from app.services.snapshot import get_snapshot


def test_detection_generates_alerts_from_data_driven_rules(ctx):
    res = al.run_detection(actor="test")
    types = {a.alert_type for a in Alert.query.all()}
    assert {"STOCKOUT", "EXCESS", "PO_DELAY", "ETA_CHANGE", "EXPIRY", "OBSOLESCENCE"} <= types
    assert res["active_alerts"] > 0
    a = Alert.query.filter_by(alert_type="STOCKOUT").order_by(Alert.priority_score.desc()).first()
    assert a.severity in ("MEDIUM", "HIGH", "CRITICAL") and a.owner and a.recommended_action and a.metrics and a.impact is not None and a.dedupe_key
    assert a.status == "New" or a.status in al.STATUSES


def test_detection_is_idempotent_deduplicates_and_counts_occurrences(ctx):
    al.run_detection()
    n = Alert.query.count()
    al.run_detection()
    assert Alert.query.count() == n                                        # same conditions → same alerts, not duplicates
    assert max(a.occurrences for a in Alert.query.all()) >= 2
    keys = [a.dedupe_key for a in Alert.query.all()]
    assert len(keys) == len(set(keys))


def test_rule_edits_change_detection_and_disabled_rules_do_not_fire(ctx):
    r = Rule.query.filter_by(code="EXCESS_INVENTORY").one()
    r.enabled = False
    db.session.commit()
    al.run_detection()
    assert not Alert.query.filter_by(rule_code="EXCESS_INVENTORY").filter(Alert.status.notin_(["Resolved", "Dismissed"])).count()       # auto-resolved
    r.enabled = True
    db.session.commit()
    al.run_detection()
    assert Alert.query.filter_by(rule_code="EXCESS_INVENTORY").filter(Alert.status == "New").count() > 0


def test_planner_priority_score_is_transparent(ctx):
    a = Alert.query.filter(Alert.priority_breakdown.isnot(None)).order_by(Alert.priority_score.desc()).first()
    parts = a.priority_breakdown
    assert set(parts) == {"financial", "service", "production", "customer", "severity", "time"}
    assert sum(p["contribution"] for p in parts.values()) == pytest.approx(a.priority_score)
    assert all(0 <= p["score"] <= 100 for p in parts.values())
    hi, _ = al.priority_score({"revenue_at_risk": 1e7, "service_impact": 0.2, "customers": 3}, "CRITICAL", 1, S.get("weights.priority"))
    lo, _ = al.priority_score({"revenue_at_risk": 1e3}, "LOW", 60, S.get("weights.priority"))
    assert hi > lo


def test_root_cause_clustering_uses_shared_dimensions_not_text(ctx):
    al.run_detection()
    port = Incident.query.filter(Incident.cluster_key.like("port:%")).first()
    assert port, "the planted port delay should form a port incident"
    assert len(port.alerts) >= 2 and "Nhava Sheva" in port.cluster_key
    for a in port.alerts:
        dims = a.dims
        assert port.cluster_key.split(":", 1)[1] in (dims.get("port") or []) or set(dims.get("shipment") or []) & set(port.dims["shipments"]) or set(dims.get("po") or []) & set(port.dims["pos"]) \
            or set(dims.get("supplier") or []) & set(port.dims["suppliers"]) or set(dims.get("lane") or []) & set(port.dims["lanes"])
    ex = port.explain
    assert {"what", "why", "when", "where", "who", "how_big", "options", "if_nothing"} <= set(ex)
    assert port.impact["revenue_at_risk"] >= 0 and port.impact["affected_skus"]
    n_alerts_in_incidents = Alert.query.filter(Alert.incident_id.isnot(None), Alert.status.in_(al.OPEN_STATUSES)).count()
    assert Incident.query.count() < n_alerts_in_incidents                      # fatigue reduction: fewer incidents than symptom alerts


def test_unrelated_alerts_are_not_clustered():
    class Dummy:
        def __init__(self, dims, tti=5):
            self.dims, self.time_to_impact_days = dims, tti
    a, b = Dummy({"sku": "X", "node": "N1"}), Dummy({"sku": "Y", "node": "N1"})
    assert incident_service._tokens(a) == [] and incident_service._tokens(b) == []            # shared node/sku alone is not a causal link


def test_alert_status_workflow_is_audited(ctx):
    a = Alert.query.filter(Alert.status == "New").first()
    al.set_status(a.id, "Acknowledged", "tester", "seen")
    db.session.commit()
    assert Alert.query.get(a.id).status == "Acknowledged"
    assert AuditLog.query.filter_by(entity_type="Alert", entity_id=str(a.id), event="alert_status").count() == 1
    with pytest.raises(ValueError):
        al.set_status(a.id, "Exploded", "tester")


# ---- actions / autonomy / approvals -----------------------------------------------------------------------------------------------
def _pair(pred):
    snap = get_snapshot()
    return next(k for k, r in snap.results.items() if pred(snap.inputs[k], r))


def _new_action(typ="CREATE_PO", **kw):
    k = _pair(lambda i, r: i.supplier_id and i.item.get("criticality") == "Medium" and r.d_mean > 0 and (i.moq or 1) >= 1)
    inp = get_snapshot().inputs[k]
    qty = kw.pop("qty", float(inp.moq or 10))
    return A.create_action(typ, item_id=k[0], location_id=k[1], qty=qty, created_by="planner", **kw), k


def test_action_pipeline_simulates_checks_policy_and_requires_approval(ctx):
    S.set_value("autonomy.default_level", 1)
    a, _ = _new_action()
    db.session.commit()
    assert a.status == "PENDING_APPROVAL" and a.approval_required and a.simulation["current"] and a.simulation["expected"] and a.policy_check["checks"]
    assert a.simulation["expected"]["position"] > a.simulation["current"]["position"]                  # the PO raises position
    assert a.simulation["delta"]["working_capital"] > 0 and a.autonomy["level"] <= 1


def test_moq_violation_blocks_approval(ctx):
    a, k = _new_action(qty=1.0)
    db.session.commit()
    inp = get_snapshot().inputs[k]
    if inp.moq and inp.moq > 1:
        assert a.policy_check["blocking"]
        with pytest.raises(A.ActionError):
            A.approve(a.id, "boss", "Administrator")


def test_role_based_approval_limits(ctx):
    a, _ = _new_action()
    a.required_role = "Executive"
    db.session.commit()
    with pytest.raises(A.ActionError, match="requires Executive"):
        A.approve(a.id, "planner", "Inventory Planner")
    with pytest.raises(A.ActionError):
        A.approve(a.id, "mgr", "Supply Chain Manager")
    if not a.policy_check["blocking"]:
        out = A.approve(a.id, "exec", "Executive", "ok")
        assert out.status in ("VERIFIED", "EXECUTED") and out.executions and out.executions[-1].is_mock


def test_mock_execution_is_always_labelled_mock_and_never_live(ctx):
    S.set_value("execution.mode", "SIMULATION_ONLY")
    a, _ = _new_action()
    if a.policy_check["blocking"]:
        pytest.skip("policy blocked")
    A.approve(a.id, "admin", "Administrator")
    ex = Execution.query.filter_by(action_id=a.id).one()
    assert ex.is_mock and ex.external_ref.startswith("MOCK-") and "SIMULATION" in ex.response["note"]
    S.set_value("execution.mode", "LIVE")
    b, _ = _new_action()
    db.session.commit()
    b.status = "APPROVED"
    with pytest.raises(A.ActionError, match="no live connector"):
        A.execute(b)
    S.set_value("execution.mode", "SIMULATION_ONLY")


def test_autonomy_rules_allow_guardrailed_auto_execution_and_deny_wins(ctx):
    S.set_value("autonomy.default_level", 1)
    snap = get_snapshot()
    k = _pair(lambda i, r: r.pos["available"] > 100 and i.item.get("criticality") != "Critical" and i.loc.get("loc_type") in ("CDC", "RDC", "PLANT"))
    a = Action(action_type="TRANSFER_STOCK", item_id=k[0], location_id=k[1], qty=10, cost=20000, confidence=0.9)
    d = A.decide_autonomy(a)
    assert d["level"] == 3 and d["auto"] and d["decision"] == "AUTO_EXECUTE"                      # "auto-approve transfers below ₹50,000"
    big = Action(action_type="TRANSFER_STOCK", item_id=k[0], location_id=k[1], qty=10, cost=80000, confidence=0.9)
    assert not A.decide_autonomy(big)["auto"]                                                     # above the guardrail
    unsure = Action(action_type="TRANSFER_STOCK", item_id=k[0], location_id=k[1], qty=10, cost=20000, confidence=0.3)
    assert not A.decide_autonomy(unsure)["auto"]                                                  # below min confidence
    po = Action(action_type="CREATE_PO", item_id=k[0], location_id=k[1], qty=1, cost=900000, confidence=0.9)
    assert A.decide_autonomy(po)["level"] <= 2 and A.decide_autonomy(po)["required_role"] in ("Executive",)
    crit = _pair(lambda i, r: i.item.get("criticality") == "Critical")
    ca = Action(action_type="TRANSFER_STOCK", item_id=crit[0], location_id=crit[1], qty=1, cost=1000, confidence=0.99)
    dc = A.decide_autonomy(ca)
    assert not dc["auto"] and any(m["effect"] == "NEVER_AUTO" for m in dc["matched_rules"])       # deny wins over allow
    rel = Action(action_type="RELEASE_STOCK", item_id=k[0], location_id=k[1], qty=1, cost=0, confidence=0.99)
    assert not A.decide_autonomy(rel)["auto"]                                                     # never auto-execute quality release / quarantine
    S.set_value("autonomy.default_level", 4)
    assert not A.decide_autonomy(ca)["auto"]                                                      # even at level 4, NEVER_AUTO holds
    S.set_value("autonomy.default_level", 1)


def test_auto_approved_transfer_is_executed_in_simulation_and_audited(ctx):
    S.set_value("execution.mode", "SIMULATION_ONLY")
    snap = get_snapshot()
    k = _pair(lambda i, r: r.pos["available"] > 100 and i.item.get("criticality") != "Critical" and i.loc.get("loc_type") in ("CDC", "RDC", "PLANT") and r.ss < 50)
    others = [kk for kk in snap.keys_for_item(k[0]) if kk != k]
    if not others:
        pytest.skip("no donor node")
    donor = others[0]
    a = A.create_action("TRANSFER_STOCK", item_id=k[0], location_id=k[1], qty=1, cost=100, confidence=0.9, created_by="auto", payload={"from_location_id": donor[1], "arrival_days": 2})
    db.session.commit()
    if a.autonomy["auto"] and not a.policy_check["blocking"]:
        assert a.status in ("VERIFIED", "EXECUTED") and a.approvals[0].approver == "AUTONOMY-POLICY"
    assert AuditLog.query.filter_by(entity_type="Action", entity_id=a.action_no).count() >= 2


def test_reject_escalate_modify(ctx):
    S.set_value("autonomy.default_level", 1)
    a, _ = _new_action()
    db.session.commit()
    A.escalate(a.id, "planner", "Inventory Planner", "needs manager")
    assert a.status == "ESCALATED" and a.required_role in ("Supply Chain Manager", "Executive")
    m = A.modify(a.id, "mgr", "Supply Chain Manager", qty=(a.qty or 10) * 2)
    assert m.qty > 0 and m.simulation["expected"]
    r = A.reject(a.id, "mgr", "Supply Chain Manager", "no budget")
    assert r.status == "REJECTED" and [x.decision for x in r.approvals][-1] == "REJECT"
    with pytest.raises(A.ActionError):
        A.approve(a.id, "mgr", "Administrator")


def test_missing_sku_and_bad_quantity_rejected(ctx):
    with pytest.raises(A.ActionError):
        A.create_action("CREATE_PO", item_id=999999, location_id=1, qty=5)
    k = next(iter(get_snapshot().results))
    with pytest.raises(A.ActionError):
        A.create_action("CREATE_PO", item_id=k[0], location_id=k[1], qty=0)
    with pytest.raises(A.ActionError):
        A.create_action("TELEPORT", item_id=k[0], location_id=k[1], qty=1)
