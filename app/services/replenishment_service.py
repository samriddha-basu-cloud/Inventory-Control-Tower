"""EOQ, practical order quantity and the twelve replenishment policies.

EOQ = √(2·D·S / H)   D annual demand, S cost per order, H annual holding cost per unit (= unit cost × holding rate).

Every policy function receives one context dict (`ctx`) and returns a dict with the raw (theoretical) quantity;
`practical_qty()` then applies MOQ, order multiple and maximum-quantity constraints so the theoretical and the
practical quantity are always both visible.
"""
from __future__ import annotations

import math

POLICIES: dict[str, dict] = {
    "MIN_MAX": {"name": "Min-Max", "rule": "If position ≤ Min → order up to Max"},
    "ROP": {"name": "Reorder Point (s,Q)", "rule": "If position ≤ ROP → order Q (EOQ) × n until position > ROP"},
    "FIXED_QTY": {"name": "Fixed Order Quantity", "rule": "If position ≤ ROP → order the fixed quantity"},
    "PERIODIC": {"name": "Periodic Review (R,S)", "rule": "At each review order S − position, S = d̄(L+R)+SS"},
    "ORDER_UP_TO": {"name": "Order-Up-To (s,S)", "rule": "If position ≤ s → order S − position"},
    "BASE_STOCK": {"name": "Base Stock", "rule": "Whenever position < base stock, order the difference (S = d̄·L + SS)"},
    "L4L": {"name": "Lot-for-Lot", "rule": "Order exactly the projected net requirement, when needed"},
    "KANBAN": {"name": "Kanban", "rule": "Replenish emptied containers; cards N = ⌈d̄(L+safety)(1+α)/container⌉"},
    "JIT": {"name": "Just-in-Time", "rule": "Cover demand over lead time + short window, no buffer"},
    "JIS": {"name": "Just-in-Sequence", "rule": "Order exactly the sequenced requirements inside the sequencing horizon"},
    "MRP": {"name": "MRP-style", "rule": "Time-phased gross-to-net; planned order releases offset by lead time"},
    "DRP": {"name": "DRP-style", "rule": "MRP netting at a distribution node incl. downstream demand; source = upstream node"},
}


def eoq(annual_demand: float, order_cost: float, unit_cost: float, holding_rate: float,
        storage_cost_per_unit: float = 0.0) -> float:
    H = max(unit_cost or 0.0, 0.0) * max(holding_rate, 0.0) + max(storage_cost_per_unit, 0.0)
    if annual_demand <= 0 or order_cost <= 0 or H <= 0:
        return 0.0
    return math.sqrt(2.0 * annual_demand * order_cost / H)


def practical_qty(q: float, moq: float | None = None, multiple: float | None = None,
                  max_qty: float | None = None) -> dict:
    """Round a theoretical quantity up to MOQ / order multiple, then check the maximum."""
    notes: list[str] = []
    violations: list[str] = []
    if q is None or q <= 1e-9:
        return {"qty": 0.0, "theoretical": max(q or 0.0, 0.0), "notes": [], "violations": []}
    qq = float(q)
    if moq and qq < moq:
        notes.append(f"raised {qq:,.0f} → MOQ {moq:,.0f}")
        qq = float(moq)
    if multiple and multiple > 0:
        k = math.ceil(qq / multiple - 1e-9)
        if abs(k * multiple - qq) > 1e-9:
            notes.append(f"rounded {qq:,.0f} → multiple of {multiple:,.0f}")
        qq = k * multiple
    if max_qty is not None and qq > max_qty + 1e-9:
        cap = max_qty
        if multiple and multiple > 0:
            cap = math.floor(max_qty / multiple + 1e-9) * multiple
        if moq and cap < moq:
            violations.append(f"Maximum order size {max_qty:,.0f} is below MOQ {moq:,.0f}: no feasible order quantity.")
            return {"qty": 0.0, "theoretical": float(q), "notes": notes, "violations": violations}
        notes.append(f"capped {qq:,.0f} → {cap:,.0f} by capacity/maximum")
        qq = cap
    return {"qty": qq, "theoretical": float(q), "notes": notes, "violations": violations}


def mrp_plan(opening: float, receipts_daily, demand_daily, ss: float, L: float, moq=None, multiple=None,
             cap=None) -> list[dict]:
    """Day-by-day gross-to-net. Whenever projected inventory would fall below SS a planned receipt is created
    at that day (release = day − L). If the release date is already in the past the order is flagged late and
    receipt is pushed out to today + L."""
    n = len(demand_daily)
    inv = float(opening)
    planned: list[dict] = []
    incoming = [0.0] * (n + int(math.ceil(L)) + 2)
    for t in range(n):
        inv += receipts_daily[t] + incoming[t] - demand_daily[t]
        if inv < ss - 1e-9:
            need = ss - inv
            pq = practical_qty(need, moq, multiple, cap)["qty"] or need
            release = t - L
            late = release < 0
            receipt_day = t if not late else int(math.ceil(L))
            if receipt_day < len(incoming):
                incoming[receipt_day] += pq
            if receipt_day == t:
                inv += pq
            planned.append({"release_day": max(0, int(round(release))), "receipt_day": int(receipt_day), "qty": pq,
                            "late": late, "shortfall_day": t})
    return planned


def recommend(policy: str, params: dict, c: dict) -> dict:
    """Evaluate one replenishment policy.

    ctx keys: position, d, L, R, ss, rop, eoq, review_due, sequenced_req, rows(mrp planned), unit
    """
    policy = (policy or "ROP").upper()
    if policy not in POLICIES:
        raise ValueError(f"Unknown replenishment policy '{policy}'")
    pos, d, L, R, ss, rop, Q = c["position"], c["d"], c["L"], c.get("R", 0), c["ss"], c["rop"], max(c.get("eoq", 0), 0)
    out = {"policy": policy, "name": POLICIES[policy]["name"], "rule": POLICIES[policy]["rule"],
           "triggered": False, "raw_qty": 0.0, "trigger_level": None, "order_up_to": None, "planned": None,
           "explanation": ""}

    def multiples(shortfall_to: float, q: float) -> float:
        if q <= 0:
            return max(shortfall_to - pos, 0)
        return q * math.ceil(max(shortfall_to - pos, 0) / q - 1e-9) if pos <= shortfall_to else 0.0

    if policy == "MIN_MAX":
        mn = params.get("min") if params.get("min") is not None else rop
        mx = params.get("max") if params.get("max") is not None else rop + Q
        out.update(trigger_level=mn, order_up_to=mx)
        if pos <= mn:
            out.update(triggered=True, raw_qty=max(mx - pos, 0))
        out["explanation"] = f"position {pos:,.0f} vs Min {mn:,.0f}; Max {mx:,.0f}"
    elif policy == "ROP":
        out["trigger_level"] = rop
        if pos <= rop:
            out.update(triggered=True, raw_qty=multiples(rop, Q if Q > 0 else params.get("fixed_qty", 0)) or Q)
        out["explanation"] = f"position {pos:,.0f} vs ROP {rop:,.0f}; Q (EOQ) {Q:,.0f}"
    elif policy == "FIXED_QTY":
        fq = params.get("fixed_qty") or Q
        out["trigger_level"] = rop
        if pos <= rop:
            out.update(triggered=True, raw_qty=multiples(rop, fq) or fq)
        out["explanation"] = f"position {pos:,.0f} vs ROP {rop:,.0f}; fixed qty {fq:,.0f}"
    elif policy == "PERIODIC":
        S = params.get("order_up_to") or (d * (L + (params.get("review_days") or R)) + ss)
        out.update(order_up_to=S, trigger_level=S)
        if c.get("review_due", True) and pos < S:
            out.update(triggered=True, raw_qty=S - pos)
        out["explanation"] = f"order-up-to S = {S:,.0f} (d̄(L+R)+SS); position {pos:,.0f}"
    elif policy == "ORDER_UP_TO":
        s = params.get("s") if params.get("s") is not None else rop
        S = params.get("order_up_to") or (rop + Q)
        out.update(order_up_to=S, trigger_level=s)
        if pos <= s:
            out.update(triggered=True, raw_qty=max(S - pos, 0))
        out["explanation"] = f"position {pos:,.0f} vs s {s:,.0f}; S {S:,.0f}"
    elif policy == "BASE_STOCK":
        S = params.get("base_stock") or (d * L + ss)
        out.update(order_up_to=S, trigger_level=S)
        if pos < S:
            out.update(triggered=True, raw_qty=S - pos)
        out["explanation"] = f"base stock {S:,.0f} (d̄·L+SS); position {pos:,.0f}"
    elif policy == "KANBAN":
        cont = params.get("container_qty") or max(Q / 4, 1)
        alpha = params.get("alpha", 0.1)
        safety_days = params.get("safety_days", 1)
        cards = params.get("cards") or max(1, math.ceil(d * (L + safety_days) * (1 + alpha) / cont))
        target = cards * cont
        out.update(order_up_to=target, trigger_level=target - cont, cards=cards, container_qty=cont)
        if pos <= target - cont:
            out.update(triggered=True, raw_qty=cont * math.floor((target - pos) / cont + 1e-9))
        out["explanation"] = f"{cards} cards × {cont:,.0f}/container = {target:,.0f}; position {pos:,.0f}"
    elif policy == "JIT":
        window = params.get("window_days", 2)
        target = d * (L + window)
        out.update(order_up_to=target, trigger_level=target)
        if pos < target:
            out.update(triggered=True, raw_qty=target - pos)
        out["explanation"] = f"cover {L + window:.1f} days (L + {window}): target {target:,.0f}; position {pos:,.0f}"
    elif policy == "JIS":
        req = c.get("sequenced_req", 0.0)
        out.update(order_up_to=req, trigger_level=req)
        cover = c.get("usable_plus_inbound_in_horizon", pos)
        if cover < req:
            out.update(triggered=True, raw_qty=req - cover)
        out["explanation"] = f"sequenced requirements in horizon {req:,.0f}; covered by stock+inbound {cover:,.0f}"
    else:  # MRP / DRP
        planned = c.get("planned", [])
        out["planned"] = planned
        due = [p for p in planned if p["release_day"] <= max(R, 0)]
        if due:
            out.update(triggered=True, raw_qty=sum(p["qty"] for p in due))
        out["explanation"] = f"{len(planned)} planned order(s) in horizon; {len(due)} due for release now"
        if policy == "DRP":
            out["source"] = "UPSTREAM_NODE"
    return out
