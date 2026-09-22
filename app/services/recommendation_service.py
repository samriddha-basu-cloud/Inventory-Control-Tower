"""Turns alerts into explainable recommendations (with alternatives, confidence and data quality).

Each recommendation stores: the decision explainer (why this quantity), all options with feasibility/constraint
violations and engine-computed AFTER states, confidence + its components, and the chosen option. Nothing is executed
here - actions are created from recommendations and go through simulation → policy → approval (action_service).
"""
from __future__ import annotations

from ..extensions import db
from ..models import Alert, Recommendation
from . import industry_service, optimization_service as opt
from . import settings_service as S
from .snapshot import Snapshot

KIND = {"CREATE_PO": "REPLENISH", "EXPEDITE_PO": "EXPEDITE", "TRANSFER_STOCK": "TRANSFER", "DO_NOTHING": "MONITOR"}


def generate(snap: Snapshot, top_n: int = 45) -> int:
    Recommendation.query.filter(Recommendation.status == "OPEN").delete(synchronize_session=False)
    db.session.flush()
    n = 0
    alerts = (Alert.query.filter(Alert.status.in_(["New", "Acknowledged", "Investigating", "Action Proposed"]), Alert.suppressed.is_(False))
              .order_by(Alert.priority_score.desc()).all())
    stock = [a for a in alerts if a.alert_type == "STOCKOUT" and a.item_id][:top_n]
    for a in stock:
        key = (a.item_id, a.location_id)
        if key not in snap.results:
            continue
        r, inp = snap.results[key], snap.inputs[key]
        res = opt.generate_options(snap, key)
        best = res["recommended"]
        if best and best["type"] == "DO_NOTHING":
            continue        # nothing to recommend: monitoring is not an action
        expl = opt.decision_explainer(inp, r)
        if best:
            kind = KIND.get(best["type"], "REPLENISH")
            title = f"{best['label']}: {best['qty']:,.0f} × {inp.sku} → {inp.loc_code}" if best["type"] != "DO_NOTHING" else f"Monitor {inp.sku} @ {inp.loc_code}"
            summary = (f"Stock-out probability {r.stockout_prob:.0%} → {best['after']['stockout_prob']:.0%} after action; incremental cost {best['incremental_cost']:,.0f}; "
                       f"arrival in {best['arrival_days']} d." if best.get("arrival_days") else "Accept the expected loss.")
            cost = best["incremental_cost"]
        else:
            kind, title = "ESCALATE", f"No feasible option for {inp.sku} @ {inp.loc_code} - escalate"
            summary = res["note"] or "Escalate for manual decision."
            cost = 0.0
        db.session.add(Recommendation(kind=kind, item_id=inp.item_id, location_id=inp.location_id, title=title, summary=summary, cost=cost,
                                      payload={"explainer": expl, "options": _clean(res["options"]), "recommended": _clean_one(best), "gap_qty": res["gap_qty"],
                                               "confidence_parts": r.confidence_parts, "risk": r.risk_level, "escalate": res["escalate"], "note": res["note"],
                                               "weights": res["weights"]},
                                      confidence=r.confidence, data_quality=r.data_quality, priority_score=a.priority_score, alert_id=a.id))
        n += 1
    # obsolescence / ECO reviews -------------------------------------------------------------------------------
    eco = {row["item_id"]: row for row in industry_service.eco_analysis(snap)}
    for a in [x for x in alerts if x.alert_type == "OBSOLESCENCE"][:12]:
        row = eco.get(a.item_id)
        r = snap.results.get((a.item_id, a.location_id))
        if not r:
            continue
        if row:
            title = f"ECO {row['sku']} → {row['successor']}: {row['recommendation']} ({row['leftover']:,.0f} left over)"
            summary = row["why"]
            payload = {"eco": {k: v for k, v in row.items() if k not in ("deficit_nodes", "surplus_nodes")}, "deficit_nodes": row["deficit_nodes"], "surplus_nodes": row["surplus_nodes"]}
        else:
            title = f"Review obsolescence: {snap.inputs[(a.item_id, a.location_id)].sku} @ {a.location.code}"
            summary = f"{r.inv_class.replace('_', '-').title()}; exposure {r.obsolescence_exposure:,.0f}. Options: sell-through/markdown, return-to-vendor, rework, donate; disposal only after human review."
            payload = {"class": r.inv_class, "on_hand_value": r.on_hand_value}
        db.session.add(Recommendation(kind="REVIEW_OBSOLESCENCE", item_id=a.item_id, location_id=a.location_id, title=title, summary=summary, payload=payload,
                                      confidence=r.confidence, data_quality=r.data_quality, priority_score=a.priority_score, cost=0.0, alert_id=a.id))
        n += 1
    for a in [x for x in alerts if x.alert_type == "EXPIRY"][:10]:
        key = (a.item_id, a.location_id)
        r = snap.results.get(key)
        if not r:
            continue
        inp = snap.inputs[key]
        from . import expiry_service as ex
        picks = ex.fefo_pick(inp.lots, min(r.pos["usable_on_hand"], max(r.at_risk_qty, 1.0)), snap.today, require_released=True)
        db.session.add(Recommendation(kind="REVIEW_EXPIRY", item_id=a.item_id, location_id=a.location_id,
                                      title=f"Review expiry: {inp.sku} @ {inp.loc_code} ({r.expiry_status.lower()})",
                                      summary=f"{r.at_risk_qty + r.expired_qty:,.0f} units expire before consumption. Prioritise FEFO picks, transfer to faster-moving nodes, or review markdown.",
                                      payload={"fefo_plan": [{**p, "expiry": p["expiry"].isoformat() if p["expiry"] else None} for p in picks["picks"]], "skipped": picks["skipped"]},
                                      confidence=r.confidence, data_quality=r.data_quality, priority_score=a.priority_score, cost=0.0, alert_id=a.id))
        n += 1
    # transfers from the rebalancing engine -------------------------------------------------------------------------
    plan = opt.rebalancing_plan(snap)
    for t in sorted(plan["transfers"], key=lambda x: -x["value"])[:12]:
        db.session.add(Recommendation(kind="TRANSFER", item_id=t["item_id"], location_id=t["to_id"],
                                      title=f"Rebalance {t['qty']:,.0f} × {t['sku']}: {t['from']} → {t['to']}",
                                      summary=f"Moves excess from {t['from']} to shortage at {t['to']} (ETA {t['eta_days']:.1f} d, freight {t['freight_cost']:,.0f}); "
                                              f"receiver stock-out probability {t['before']['to_prob']:.0%} → {t['after']['to_prob']:.0%}.",
                                      payload={"transfer": _clean_one(t)}, confidence=0.75, data_quality=snap.freshness["score"], priority_score=40.0 + min(30.0, t["value"] / 50000.0),
                                      cost=t["freight_cost"]))
        n += 1
    db.session.flush()
    for rec in Recommendation.query.filter(Recommendation.rec_no.is_(None)).all():
        rec.rec_no = f"REC-{rec.id:06d}"
    return n


def _clean_one(o):
    if o is None:
        return None
    import datetime as _dt
    if isinstance(o, dict):
        return {k: _clean_one(v) for k, v in o.items() if k != "arc"}
    if isinstance(o, (list, tuple)):
        return [_clean_one(v) for v in o]
    if isinstance(o, (_dt.date, _dt.datetime)):
        return o.isoformat()
    if isinstance(o, float) and (o != o or o in (float("inf"), float("-inf"))):
        return None
    return o


def _clean(opts):
    return [_clean_one(o) for o in opts]
