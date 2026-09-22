"""Constrained allocation under scarcity (LP).

    maximise   Σ w_j · x_j                          (w_j = composite priority score of demand j)
    subject to Σ x_j ≤ S                            (supply)
               0 ≤ x_j ≤ d_j                        (never over-allocate a demand)
               x_j ≥ floor_j                        (optional minimum-fill guarantee, share of min(d_j, fair share))

With floors the model spreads scarce stock instead of starving low-priority demands completely.
If the floors alone exceed supply the problem is infeasible and the result explains by how much.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import linprog


def solve_allocation(supply: float, demands: list[dict], min_fill: float = 0.0) -> dict:
    """demands: [{'id','qty','score'}]. Returns {'status','allocations':{id:qty},'explanation'}."""
    n = len(demands)
    if n == 0 or supply <= 0:
        return {"status": "OPTIMAL", "allocations": {d["id"]: 0.0 for d in demands}, "unmet": {d["id"]: d["qty"] for d in demands},
                "explanation": "No supply or no demand.", "objective": 0.0}
    d = np.array([x["qty"] for x in demands], float)
    w = np.array([max(x.get("score", 0.0), 1e-6) for x in demands], float)
    fair = supply / n
    floor = np.minimum(d, fair) * min_fill
    if floor.sum() > supply + 1e-9:
        return {"status": "INFEASIBLE", "allocations": {}, "unmet": {},
                "explanation": f"Minimum-fill guarantees need {floor.sum():,.0f} units but only {supply:,.0f} are available. "
                               f"Lower the minimum-fill share below {supply / max(floor.sum() / max(min_fill, 1e-9), 1e-9):.0%} or add supply.",
                "objective": None}
    res = linprog(c=-w, A_ub=np.ones((1, n)), b_ub=[supply], bounds=list(zip(floor, d)), method="highs")
    if not res.success:
        return {"status": "INFEASIBLE", "allocations": {}, "unmet": {}, "explanation": res.message, "objective": None}
    x = np.maximum(res.x, 0.0)
    return {"status": "OPTIMAL", "allocations": {dm["id"]: float(x[i]) for i, dm in enumerate(demands)},
            "unmet": {dm["id"]: float(d[i] - x[i]) for i, dm in enumerate(demands)},
            "explanation": f"Allocated {x.sum():,.0f} of {supply:,.0f} available across {n} demands (min-fill {min_fill:.0%}).",
            "objective": float(-res.fun)}
