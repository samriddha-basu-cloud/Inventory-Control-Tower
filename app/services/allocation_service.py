"""Allocation policies, order pegging and safe commitment of stock.

Policies are configuration (ControlPolicy type='allocation', hierarchical): FIFO, priority customer, revenue,
criticality, service level, geography, margin, contractual priority, or a weighted composite of all of them.
Pegging links supplies (on-hand lots, POs, TOs, production) to demands (sales orders, backorders, production
requirements); a unit of supply is never pegged twice and a demand is never pegged beyond its quantity.
"""
from __future__ import annotations

from datetime import date, timedelta

from ..extensions import db
from ..models import Allocation, ControlPolicy, Item, Location, Lot, Peg
from ..optimization.allocation import solve_allocation
from ..rules.policies import resolve
from . import audit_service as audit
from . import ledger_service as ledger
from . import settings_service as S
from .snapshot import get_snapshot

CRIT = {"Critical": 1.0, "High": 0.75, "Medium": 0.5, "Low": 0.25}
DEFAULT_WEIGHTS = {"priority_customer": 0.30, "revenue": 0.20, "margin": 0.10, "criticality": 0.10, "service_level": 0.10,
                   "geography": 0.05, "contract": 0.15, "fifo": 0.0}
METHODS = ["fifo", "priority", "revenue", "criticality", "service_level", "geography", "margin", "contract", "weighted", "fefo_priority"]


class AllocationError(ValueError):
    pass


class OverAllocationError(AllocationError):
    pass


def score_demand(d: dict, ctx: dict, weights: dict) -> tuple[float, dict]:
    """Composite score in [0,1]; returns (score, per-attribute contributions) so the ranking is explainable."""
    parts = {
        "priority_customer": (5 - d.get("customer_priority", d.get("priority", 3))) / 4.0,
        "revenue": d.get("value", 0.0) / ctx["max_value"] if ctx["max_value"] else 0.0,
        "margin": ctx["margin"],
        "criticality": CRIT.get(ctx["criticality"], 0.5),
        "service_level": min(1.0, max(0.0, -d.get("due_day", 0)) / 14.0) + (0.3 if d.get("overdue") else 0.0),
        "geography": 1.0 if d.get("same_region") else 0.3,
        "contract": 1.0 if d.get("contract") else 0.0,
        "fifo": ctx["fifo_rank"].get(d.get("ref"), 0.0),
    }
    tw = sum(weights.values()) or 1.0
    contrib = {k: parts[k] * weights.get(k, 0.0) / tw for k in parts}
    return sum(contrib.values()), contrib


def rank_demands(demands: list[dict], item: dict, loc: dict, params: dict, customers: dict) -> list[dict]:
    method = params.get("method", "weighted")
    weights = {**DEFAULT_WEIGHTS, **(params.get("weights") or {})}
    price, cost = item.get("selling_price") or 0.0, item.get("unit_cost") or 0.0
    margin = ((price - cost) / price) if price else 0.0
    max_value = max([d.get("value", 0.0) for d in demands] or [0.0])
    by_date = sorted(demands, key=lambda d: (str(d.get("order_date") or ""), d.get("due_day", 0)))
    n = max(len(by_date) - 1, 1)
    fifo_rank = {d["ref"]: 1.0 - i / n for i, d in enumerate(by_date)}
    ctx = {"max_value": max_value, "margin": margin, "criticality": item.get("criticality"), "fifo_rank": fifo_rank}
    out = []
    for d in demands:
        cust = customers.get(d.get("customer_id"), {})
        d = {**d, "contract": cust.get("contract_priority", False), "same_region": cust.get("region") == loc.get("region"),
             "overdue": d.get("due_day", 0) < 0}
        s, contrib = score_demand(d, ctx, weights)
        single = {"fifo": ctx["fifo_rank"].get(d["ref"], 0), "priority": (5 - d.get("customer_priority", 3)) / 4,
                  "revenue": d.get("value", 0) / max_value if max_value else 0, "criticality": CRIT.get(item.get("criticality"), .5),
                  "service_level": min(1.0, max(0.0, -d.get("due_day", 0)) / 14.0), "geography": 1.0 if d["same_region"] else 0.3,
                  "margin": margin, "contract": 1.0 if d["contract"] else 0.0}
        d["score"] = single[method] if method in single else s
        d["score_parts"] = contrib
        out.append(d)
    out.sort(key=lambda d: (-d["score"], d.get("due_day", 0), str(d.get("order_date") or "")))
    return out


def peg_pair(inp, params: dict, customers: dict, today: date, require_released: bool = True) -> dict:
    """Peg one item-location. Returns {'pegs':[...], 'unmet':[...], 'onhand_alloc':[...]} — pure, no DB writes."""
    item, loc = inp.item, inp.loc
    demands = []
    for o in inp.orders + inp.extra.get("backorders", []):
        demands.append({**o, "type": "SO", "ref": o["ref"], "due_day": o["due_day"]})
    for r in inp.requirements:
        demands.append({"type": "PROD", "ref": r["ref"], "qty": r["qty"], "due_day": r["due_day"], "priority": r.get("priority", 3),
                        "customer_id": None, "value": r["qty"] * (item.get("unit_cost") or 0.0), "customer_priority": r.get("priority", 3)})
    ranked = rank_demands(demands, item, loc, params, customers)
    # supplies -----------------------------------------------------------------------------------------------
    supplies = []
    lots = [l for l in inp.lots if l["state"] == "UNRESTRICTED" and l["qty"] > 0]
    if require_released:
        lots = [l for l in lots if l.get("quality", "RELEASED") == "RELEASED"]
    lots = [l for l in lots if not (l.get("expiry") and l["expiry"] < today)]
    fefo = params.get("method") == "fefo_priority" or params.get("fefo", item.get("shelf_life_days") is not None)
    far = date.max
    lots.sort(key=lambda l: ((l.get("expiry") or far), (l.get("received") or far)) if fefo else ((l.get("received") or far),))
    hold = inp.committed + inp.reserved
    for l in lots:
        q = l["qty"]
        if hold > 0:
            take = min(q, hold)
            q -= take
            hold -= take
        if q > 1e-9:
            supplies.append({"type": "ONHAND", "ref": l.get("lot_no") or "STOCK", "lot_id": l.get("lot_id"), "qty": q, "day": 0})
    for ib in sorted(inp.inbound, key=lambda i: i["eta_day"]):
        supplies.append({"type": ib["kind"], "ref": ib["ref"], "qty": ib["qty"], "day": max(0, ib["eta_day"]), "lot_id": None})
    pegs, unmet = [], []
    for d in ranked:
        need = d["qty"]
        for s in supplies:
            if need <= 1e-9:
                break
            if s["qty"] <= 1e-9:
                continue
            take = min(s["qty"], need)
            s["qty"] -= take
            need -= take
            pegs.append({"supply_type": s["type"], "supply_ref": s["ref"], "lot_id": s.get("lot_id"), "demand_type": d["type"],
                         "demand_ref": d["ref"], "customer_id": d.get("customer_id"), "qty": take,
                         "expected_date": today + timedelta(days=s["day"]), "demand_date": today + timedelta(days=d["due_day"]),
                         "late": s["day"] > d["due_day"], "score": d["score"]})
        if need > 1e-9:
            unmet.append({"demand_ref": d["ref"], "qty": need, "demand_type": d["type"], "due_day": d["due_day"], "customer_id": d.get("customer_id")})
    return {"pegs": pegs, "unmet": unmet, "ranked": ranked, "remaining_supply": [s for s in supplies if s["qty"] > 1e-9]}


def _policy_params(policies, item, loc) -> dict:
    merged, _ = resolve([p for p in policies], item, loc)
    return merged or {"method": "weighted"}


def run_pegging_all(scope_items: set[int] | None = None) -> dict:
    """(Re)compute pegs and stock allocations for every item-location with demand. Manual/committed claims are kept."""
    snap = get_snapshot(force=True)
    policies = ControlPolicy.query.filter_by(policy_type="allocation", active=True).all()
    q_alloc = Allocation.query.filter(Allocation.status == "ACTIVE", Allocation.policy != "MANUAL")
    q_peg = Peg.query.filter(Peg.peg_status == "PLANNED")
    if scope_items:
        q_alloc, q_peg = q_alloc.filter(Allocation.item_id.in_(scope_items)), q_peg.filter(Peg.item_id.in_(scope_items))
    q_alloc.delete(synchronize_session=False)
    q_peg.delete(synchronize_session=False)
    db.session.flush()
    n_peg = n_alloc = n_unmet = 0
    for k, inp in snap.inputs.items():
        if scope_items and inp.item_id not in scope_items:
            continue
        if not (inp.orders or inp.requirements or inp.extra.get("backorders")):
            continue
        params = _policy_params(policies, inp.item, inp.loc)
        res = peg_pair(inp, params, snap.customers, snap.today, params.get("require_released", True))
        for p in res["pegs"]:
            db.session.add(Peg(item_id=inp.item_id, location_id=inp.location_id, supply_type=p["supply_type"], supply_ref=p["supply_ref"],
                               demand_type=p["demand_type"], demand_ref=p["demand_ref"], customer_id=p["customer_id"], qty=p["qty"],
                               expected_date=p["expected_date"], demand_date=p["demand_date"], peg_status="PLANNED", source_system="ICT"))
            n_peg += 1
            if p["supply_type"] == "ONHAND":
                db.session.add(Allocation(item_id=inp.item_id, location_id=inp.location_id, lot_id=p["lot_id"], demand_type=p["demand_type"],
                                          demand_ref=p["demand_ref"], customer_id=p["customer_id"], qty=p["qty"], alloc_type="ALLOCATED",
                                          policy=params.get("method", "weighted"), source_system="ICT"))
                n_alloc += 1
        n_unmet += len(res["unmet"])
    audit.log("DATA", "Allocation", "*", "pegging_run", {"pegs": n_peg, "allocations": n_alloc, "unmet_demands": n_unmet})
    S.bump_version()
    return {"pegs": n_peg, "allocations": n_alloc, "unmet_demands": n_unmet}


def commit_allocation(item_id: int, location_id: int, qty: float, *, demand_ref: str | None = None, demand_type: str = "MANUAL",
                      lot_id: int | None = None, alloc_type: str = "COMMITTED", customer_id: int | None = None, actor: str = "system") -> Allocation:
    """Manual/hard allocation. Refuses to over-allocate: available = usable stock − existing claims."""
    if qty <= 0:
        raise AllocationError("Allocation quantity must be positive.")
    snap = get_snapshot(force=True)
    inp = snap.results.get((item_id, location_id))
    if not inp:
        raise AllocationError("Unknown item/location: nothing to allocate.")
    avail = inp.pos["available"]
    if lot_id is not None:
        lot_qty = sum(l["qty"] for l in snap.inputs[(item_id, location_id)].lots if l["lot_id"] == lot_id and l["state"] == "UNRESTRICTED")
        lot_claims = sum(a.qty for a in Allocation.query.filter_by(item_id=item_id, location_id=location_id, lot_id=lot_id, status="ACTIVE"))
        avail = min(avail, lot_qty - lot_claims)
    if qty > avail + 1e-9:
        raise OverAllocationError(f"Impossible allocation: requested {qty:,.0f} but only {max(avail, 0):,.0f} available (usable stock minus existing claims).")
    a = Allocation(item_id=item_id, location_id=location_id, lot_id=lot_id, demand_type=demand_type, demand_ref=demand_ref, customer_id=customer_id,
                   qty=qty, alloc_type=alloc_type, policy="MANUAL", source_system="ICT")
    db.session.add(a)
    db.session.flush()
    audit.log("DATA", "Allocation", a.id, "allocation_committed", {"item_id": item_id, "location_id": location_id, "qty": qty, "type": alloc_type}, actor=actor)
    S.bump_version()
    return a


def release_allocation(alloc_id: int, actor: str = "system") -> None:
    a = Allocation.query.get(alloc_id)
    if not a or a.status != "ACTIVE":
        raise AllocationError("Allocation not found or already released.")
    a.status = "RELEASED"
    audit.log("DATA", "Allocation", a.id, "allocation_released", {"qty": a.qty}, actor=actor)
    S.bump_version()


def scarcity_plan(supply: float, demands: list[dict], min_fill: float = 0.0) -> dict:
    """Optimised (LP) split of scarce supply; see optimization/allocation.py."""
    return solve_allocation(supply, demands, min_fill)
