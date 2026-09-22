"""Multi-objective scoring and network-level balancing.

ObjectiveFunction keeps the objective transparent: each term has a sense (min/max), a configurable weight, and
a measured value; `evaluate()` returns the scalar (lower is better; 'max' terms enter negatively) plus a table
(Objective | Weight | Constraint | Result) that the UI shows verbatim.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_TERMS = {
    "stockouts": {"label": "Stock-outs (expected lost margin)", "sense": "min"},
    "holding": {"label": "Holding cost", "sense": "min"},
    "expedite": {"label": "Expedite / freight cost", "sense": "min"},
    "obsolescence": {"label": "Obsolescence exposure", "sense": "min"},
    "working_capital": {"label": "Working capital (cash tied up)", "sense": "min"},
    "carbon": {"label": "Carbon (priced kg CO2e)", "sense": "min"},
    "service": {"label": "Service level", "sense": "max"},
}


@dataclass
class ObjectiveFunction:
    weights: dict = field(default_factory=dict)
    carbon_price: float = 4.0           # currency per kg CO2e - a configurable internal shadow price, not a market price
    service_value: float = 0.0          # currency value of one full service-level point (0 = ignore)

    def evaluate(self, metrics: dict, constraints: dict | None = None) -> dict:
        """metrics keys: stockouts, holding, expedite, obsolescence, working_capital, carbon_kg, service."""
        table, total = [], 0.0
        for key, spec in DEFAULT_TERMS.items():
            w = float(self.weights.get(key, 1.0))
            val = metrics.get("carbon_kg" if key == "carbon" else key, 0.0) or 0.0
            money = val * self.carbon_price if key == "carbon" else (val * self.service_value if key == "service" else val)
            contrib = (-1 if spec["sense"] == "max" else 1) * w * money
            if key == "service" and not self.service_value:
                contrib = 0.0
            total += contrib
            table.append({"objective": spec["label"], "sense": spec["sense"], "weight": w, "raw": val, "contribution": contrib,
                          "constraint": (constraints or {}).get(key)})
        return {"score": total, "table": table}


def balance_network(per_item: list[dict], solver, **kw) -> dict:
    """per_item: [{'item': sku, 'donors':[...], 'receivers':[...], 'arcs':{...}}]; runs `solver` per item and aggregates."""
    plans, cost, unmet, flows = [], 0.0, 0.0, 0
    for it in per_item:
        res = solver(it["donors"], it["receivers"], it["arcs"], **kw)
        plans.append({"item": it["item"], **res})
        cost += res.get("total_cost") or 0.0
        unmet += sum(res.get("unmet", {}).values())
        flows += len(res.get("flows", []))
    return {"plans": plans, "total_cost": cost, "total_unmet": unmet, "transfers": flows}
