"""Inventory transfer / rebalancing as a transportation LP.

    minimise   Σ c_ij x_ij + Σ p_j u_j
    subject to Σ_j x_ij ≤ S_i                    (donor can only give its transferable excess)
               Σ_i x_ij + u_j = N_j              (receiver need is met by transfers or left unmet u_j at penalty p_j)
               0 ≤ x_ij ≤ cap_ij                 (lane/transfer capacity; blocked arcs have cap 0)

Arcs that cannot arrive in time, are restricted by policy, or exceed capacity are blocked with a reason, so the plan
never contains an infeasible movement and unmet need is always explained.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import linprog


def solve_transfers(donors: list[dict], receivers: list[dict], arcs: dict, require_full: bool = False) -> dict:
    """donors: [{'id','qty'}]; receivers: [{'id','need','penalty'}];
    arcs: {(donor_id, receiver_id): {'cost': per-unit effective cost, 'cap': max units, 'blocked': reason|None, ...}}"""
    nd, nr = len(donors), len(receivers)
    if nd == 0 or nr == 0:
        return {"status": "OPTIMAL", "flows": [], "unmet": {r["id"]: r["need"] for r in receivers}, "total_cost": 0.0,
                "explanation": ["No donors with transferable excess." if nd == 0 else "No receivers with shortage."], "blocked": []}
    var = nd * nr + nr
    c = np.zeros(var)
    ub = np.zeros(var)
    blocked = []
    for i, d in enumerate(donors):
        for j, r in enumerate(receivers):
            a = arcs.get((d["id"], r["id"]), {"cost": 0.0, "cap": 0.0, "blocked": "no lane defined"})
            idx = i * nr + j
            c[idx] = a["cost"]
            if a.get("blocked"):
                ub[idx] = 0.0
                blocked.append({"donor": d["id"], "receiver": r["id"], "reason": a["blocked"]})
            else:
                ub[idx] = min(a.get("cap", np.inf), d["qty"], r["need"])
    for j, r in enumerate(receivers):
        c[nd * nr + j] = r["penalty"]
        ub[nd * nr + j] = r["need"]
    A_ub = np.zeros((nd, var))
    for i in range(nd):
        A_ub[i, i * nr:(i + 1) * nr] = 1.0
    b_ub = [d["qty"] for d in donors]
    A_eq = np.zeros((nr, var))
    for j in range(nr):
        for i in range(nd):
            A_eq[j, i * nr + j] = 1.0
        A_eq[j, nd * nr + j] = 1.0
    b_eq = [r["need"] for r in receivers]
    res = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, bounds=list(zip(np.zeros(var), ub)), method="highs")
    if not res.success:
        return {"status": "INFEASIBLE", "flows": [], "unmet": {}, "total_cost": None, "blocked": blocked,
                "explanation": [f"Solver could not find a plan: {res.message}"]}
    x = res.x
    flows = []
    for i, d in enumerate(donors):
        for j, r in enumerate(receivers):
            q = float(x[i * nr + j])
            if q >= 0.5:
                q = float(np.floor(q + 1e-6))
                if q > 0:
                    flows.append({"donor": d["id"], "receiver": r["id"], "qty": q, "unit_cost": arcs[(d["id"], r["id"])]["cost"], "arc": arcs[(d["id"], r["id"])]})
    unmet = {r["id"]: max(0.0, r["need"] - sum(f["qty"] for f in flows if f["receiver"] == r["id"])) for r in receivers}
    expl = []
    for r in receivers:
        if unmet[r["id"]] > 0.5:
            why = [b["reason"] for b in blocked if b["receiver"] == r["id"]]
            expl.append(f"{r['id']}: {unmet[r['id']]:,.0f} units of need cannot be met by transfers" + (f" ({'; '.join(sorted(set(why)))})" if why else " (donor excess exhausted)"))
    status = "OPTIMAL"
    if require_full and any(v > 0.5 for v in unmet.values()):
        status = "INFEASIBLE"
        expl.insert(0, "No feasible plan meets the full shortage under the current constraints.")
    return {"status": status, "flows": flows, "unmet": unmet, "total_cost": float(res.fun), "explanation": expl, "blocked": blocked}
