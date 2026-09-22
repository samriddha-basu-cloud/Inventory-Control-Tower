"""Constrained multi-objective replenishment (mixed-integer LP via SciPy/HiGHS).

Decision variables per SKU i:  kᵢ ∈ ℤ≥0 (number of order multiples), yᵢ ∈ {0,1} (order placed), sᵢ ≥ 0 (units lost),
zᵢ ≥ 0 (units recovered by emergency/expedited buy), eᵢ ≥ 0 (units bought beyond need); per supplier s: tₛ ∈ ℤ≥0 trucks.
    qᵢ = multipleᵢ · kᵢ

Objective (each term visible in the result table, weights configurable):
    w_stockout·pᵢ·sᵢ  +  w_expedite·xᵢ·zᵢ  +  w_holding·hᵢ·qᵢ  +  w_wc·cᵢ·rate·qᵢ  +  w_obsolescence·oᵢ·eᵢ
    +  w_carbon·κ·gᵢ·qᵢ  +  ordering-and-freight (order cost·yᵢ + truck cost·tₛ)

Constraints: MOQ, order multiple, budget (incl. emergency buys), warehouse volume, supplier capacity, truck weight capacity, shelf-life cap,
service floor  qᵢ + zᵢ ≥ β·needᵢ, and shortage balance  sᵢ + zᵢ + qᵢ ≥ needᵢ.
Infeasibility is diagnosed by relaxing constraint families one at a time, and the minimum budget that would restore
feasibility is computed - a recommendation is never silently invalid.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.optimize import LinearConstraint, Bounds, milp
from scipy.sparse import lil_matrix

TERMS = ["stockout", "expedite", "holding", "working_capital", "obsolescence", "carbon", "ordering_freight"]


def _build(skus, suppliers, cons, weights, enforce_service, drop=frozenset(), min_spend=False):
    n, m = len(skus), len(suppliers)
    # variable layout: k(n) y(n) s(n) z(n) e(n) t(m)
    K, Y, Sx, Z, E, T = 0, n, 2 * n, 3 * n, 4 * n, 5 * n
    nv = 5 * n + m
    c = np.zeros(nv)
    sup_idx = {s["id"]: j for j, s in enumerate(suppliers)}
    rate = cons.get("capital_rate", 0.12)
    for i, s in enumerate(skus):
        mult = s["mult"]
        c[K + i] = mult * (weights["holding"] * s["holding_unit"] + weights["working_capital"] * s["unit_cost"] * rate
                           + weights["carbon"] * cons.get("carbon_price", 4.0) * s["co2_unit"])
        c[Y + i] = cons.get("order_cost", 1500.0)
        c[Sx + i] = weights["stockout"] * s["short_cost"]
        c[Z + i] = weights["expedite"] * s["expedite_unit"]
        c[E + i] = weights["obsolescence"] * s["obsolescence_unit"]
    for j, sp in enumerate(suppliers):
        c[T + j] = sp.get("truck_cost", 12000.0)
    if min_spend:
        c = np.zeros(nv)
        for i, s in enumerate(skus):
            c[K + i] = s["mult"] * s["unit_cost"]
            c[Z + i] = s["unit_cost"]
    rows, lo, hi = [], [], []

    def add(coefs: dict, l, h):
        rows.append(coefs)
        lo.append(l)
        hi.append(h)

    names = []
    for i, s in enumerate(skus):
        mult, need = s["mult"], s["need"]
        big = max(s["max_qty"], s["moq"], mult, 1.0)
        # balance: q + s + z >= need
        add({K + i: mult, Sx + i: 1, Z + i: 1}, need, np.inf)
        names.append(("balance", s["id"]))
        # excess: e >= q - need
        add({E + i: 1, K + i: -mult}, -need, np.inf)
        names.append(("excess", s["id"]))
        # MOQ / link: q >= moq*y ; q <= big*y
        if "moq" not in drop:
            add({K + i: mult, Y + i: -s["moq"]}, 0, np.inf)
            names.append(("moq", s["id"]))
        add({K + i: mult, Y + i: -big}, -np.inf, 0)
        names.append(("link", s["id"]))
        if enforce_service and "service" not in drop:
            add({K + i: mult, Z + i: 1}, cons.get("service_floor", 0.95) * need, np.inf)
            names.append(("service", s["id"]))
        if "shelf" not in drop and s.get("shelf_cap") is not None:
            add({K + i: mult}, -np.inf, s["shelf_cap"])
            names.append(("shelf", s["id"]))
        if s.get("max_qty") is not None and "capacity" not in drop:
            add({K + i: mult}, -np.inf, s["max_qty"])
            names.append(("capacity", s["id"]))
    if cons.get("budget") is not None and "budget" not in drop:
        co = {K + i: s["mult"] * s["unit_cost"] for i, s in enumerate(skus)}
        co.update({Z + i: s["unit_cost"] for i, s in enumerate(skus)})        # emergency buys are cash too
        add(co, -np.inf, cons["budget"])
        names.append(("budget", "*"))
    if cons.get("warehouse_m3") is not None and "warehouse" not in drop:
        add({K + i: s["mult"] * s["volume_m3"] for i, s in enumerate(skus)}, -np.inf, cons["warehouse_m3"])
        names.append(("warehouse", "*"))
    for j, sp in enumerate(suppliers):
        members = [i for i, s in enumerate(skus) if s["supplier"] == sp["id"]]
        if not members:
            continue
        if sp.get("capacity") is not None and "supplier" not in drop:
            add({K + i: skus[i]["mult"] for i in members}, -np.inf, sp["capacity"])
            names.append(("supplier", sp["id"]))
        if cons.get("truck_kg") and "truck" not in drop:
            co = {K + i: skus[i]["mult"] * skus[i]["weight_kg"] for i in members}
            co[T + j] = -cons["truck_kg"]
            add(co, -np.inf, 0)
            names.append(("truck", sp["id"]))
    A = lil_matrix((len(rows), nv))
    for r, co in enumerate(rows):
        for col, v in co.items():
            A[r, col] = v
    integrality = np.concatenate([np.ones(n), np.ones(n), np.zeros(3 * n), np.ones(m)])
    cap = cons.get("expedite_cap", 0.3)          # emergency/expedited recourse is limited to a share of the need
    ub = np.concatenate([[math.ceil(max(s["max_qty"], s["moq"], s["need"]) / s["mult"]) + 1 for s in skus], np.ones(n),
                         np.full(n, np.inf), [cap * s["need"] for s in skus], np.full(n, np.inf), np.full(m, 200)])
    return c, LinearConstraint(A.tocsr(), lo, hi), integrality, Bounds(np.zeros(nv), ub), names, (K, Y, Sx, Z, E, T)


def solve(skus: list[dict], suppliers: list[dict], cons: dict, weights: dict, enforce_service: bool = True, time_limit: float = 20.0) -> dict:
    """skus: id, need, moq, mult, max_qty, unit_cost, weight_kg, volume_m3, supplier, holding_unit, short_cost, expedite_unit,
    obsolescence_unit, co2_unit, shelf_cap. Returns plan + objective table + constraint table (or an infeasibility explanation)."""
    if not skus:
        return {"status": "OPTIMAL", "plan": [], "objective": [], "constraints": [], "total": 0.0, "explanation": ["Nothing to order."]}
    w = {t: float(weights.get(t, 1.0)) for t in TERMS}
    n, m = len(skus), len(suppliers)
    c, cons_obj, integ, bnds, names, (K, Y, Sx, Z, E, T) = _build(skus, suppliers, cons, w, enforce_service)
    res = milp(c, constraints=cons_obj, integrality=integ, bounds=bnds, options={"time_limit": time_limit, "mip_rel_gap": 1e-4})
    if not res.success or res.x is None:
        return {"status": "INFEASIBLE", "plan": [], "objective": [], "constraints": [], "total": None,
                "explanation": diagnose(skus, suppliers, cons, w, enforce_service)}
    x = res.x
    plan, terms = [], dict.fromkeys(TERMS, 0.0)
    rate = cons.get("capital_rate", 0.12)
    for i, s in enumerate(skus):
        q = float(round(x[K + i])) * s["mult"]
        sh, zz, ee = float(x[Sx + i]), float(x[Z + i]), float(x[E + i])
        terms["stockout"] += w["stockout"] * s["short_cost"] * sh
        terms["expedite"] += w["expedite"] * s["expedite_unit"] * zz
        terms["holding"] += w["holding"] * s["holding_unit"] * q
        terms["working_capital"] += w["working_capital"] * s["unit_cost"] * rate * q
        terms["obsolescence"] += w["obsolescence"] * s["obsolescence_unit"] * ee
        terms["carbon"] += w["carbon"] * cons.get("carbon_price", 4.0) * s["co2_unit"] * q
        terms["ordering_freight"] += cons.get("order_cost", 1500.0) * float(round(x[Y + i]))
        plan.append({"id": s["id"], "qty": q, "need": s["need"], "short_units": max(sh, 0.0), "expedited_units": max(zz, 0.0), "excess_units": max(ee, 0.0),
                     "spend": (q + max(zz, 0.0)) * s["unit_cost"], "moq": s["moq"], "multiple": s["mult"], "supplier": s["supplier"],
                     "fill": min(1.0, (q + zz) / s["need"]) if s["need"] else 1.0})
    trucks = {sp["id"]: int(round(x[T + j])) for j, sp in enumerate(suppliers)}
    terms["ordering_freight"] += sum(sp.get("truck_cost", 12000.0) * trucks[sp["id"]] for sp in suppliers)
    total = float(res.fun)
    used_spend = sum(p["spend"] for p in plan)
    constraints = [{"name": "Budget", "limit": cons.get("budget"), "used": used_spend, "binding": cons.get("budget") is not None and used_spend >= 0.999 * cons["budget"]}]
    if cons.get("warehouse_m3") is not None:
        vol = sum(p["qty"] * next(s["volume_m3"] for s in skus if s["id"] == p["id"]) for p in plan)
        constraints.append({"name": "Warehouse volume (m³)", "limit": cons["warehouse_m3"], "used": vol, "binding": vol >= 0.999 * cons["warehouse_m3"]})
    for sp in suppliers:
        if sp.get("capacity") is not None:
            u = sum(p["qty"] for p in plan if p["supplier"] == sp["id"])
            constraints.append({"name": f"Supplier capacity {sp['id']}", "limit": sp["capacity"], "used": u, "binding": u >= 0.999 * sp["capacity"]})
        if trucks.get(sp["id"]):
            constraints.append({"name": f"Trucks {sp['id']}", "limit": None, "used": trucks[sp["id"]], "binding": False})
    if enforce_service:
        constraints.append({"name": f"Service floor ({cons.get('service_floor', 0.95):.0%} of need)", "limit": cons.get("service_floor", 0.95),
                            "used": min([p["fill"] for p in plan] or [1.0]), "binding": any(abs(p["fill"] - cons.get("service_floor", 0.95)) < 1e-6 for p in plan)})
    objective = [{"term": t, "weight": w[t], "value": terms[t]} for t in TERMS]
    return {"status": "OPTIMAL", "plan": plan, "objective": objective, "constraints": constraints, "total": total, "trucks": trucks, "explanation": [
        f"Solved {n} SKUs / {m} suppliers to optimality (HiGHS MILP). Spend {used_spend:,.0f}."]}


def diagnose(skus, suppliers, cons, w, enforce_service) -> list[str]:
    """Explain infeasibility: which constraint families, when relaxed, restore feasibility; and the budget actually required."""
    msgs = ["No feasible replenishment plan exists under the current constraints."]
    families = ["budget", "warehouse", "supplier", "truck", "shelf", "capacity", "service", "moq"]
    fixes = []
    for fam in families:
        c, cons_obj, integ, bnds, _, _ = _build(skus, suppliers, cons, w, enforce_service, drop=frozenset([fam]))
        r = milp(c, constraints=cons_obj, integrality=integ, bounds=bnds, options={"time_limit": 8})
        if r.success:
            fixes.append(fam)
    label = {"budget": "the budget", "warehouse": "warehouse volume capacity", "supplier": "supplier capacity", "truck": "truck capacity",
             "shelf": "the shelf-life cap", "capacity": "the maximum order size", "service": "the service-level floor", "moq": "MOQ rules"}
    if fixes:
        msgs.append("Feasibility is restored if you relax: " + ", ".join(label[f] for f in fixes) + ".")
    else:
        msgs.append("No single constraint family explains this; a combination of constraints conflicts.")
    if cons.get("budget") is not None:
        c, cons_obj, integ, bnds, _, _ = _build(skus, suppliers, cons, w, enforce_service, drop=frozenset(["budget"]), min_spend=True)
        r = milp(c, constraints=cons_obj, integrality=integ, bounds=bnds, options={"time_limit": 8})
        if r.success:
            msgs.append(f"The cheapest plan meeting all other constraints costs {r.fun:,.0f}, above the budget of {cons['budget']:,.0f}.")
    return msgs
