"""Single-echelon baseline: every node optimizes its own safety stock
independently, ignoring what happens elsewhere in the network."""
from app.optimization.safety_stock import combined_variability


def optimize_single_echelon(nodes):
    """
    nodes: list of dicts with keys:
        node, avg_demand_daily, std_dev_demand_daily, lead_time_days,
        std_dev_lead_time_days, service_level_pct, unit_cost
    Returns per-node safety stock computed with no network coordination,
    plus network totals for later comparison against MEIO.
    """
    results = []
    total_ss_units = 0.0
    total_ss_value = 0.0
    for n in nodes:
        calc = combined_variability(
            n["avg_demand_daily"], n["std_dev_demand_daily"], n["lead_time_days"],
            n.get("std_dev_lead_time_days", 0.0), n["service_level_pct"],
        )
        ss = calc["safety_stock"]
        value = ss * n.get("unit_cost", 0.0)
        results.append({**n, "safety_stock": ss, "safety_stock_value": round(value, 2), "calc": calc})
        total_ss_units += ss
        total_ss_value += value

    return {
        "mode": "single_echelon",
        "nodes": results,
        "total_safety_stock_units": round(total_ss_units, 2),
        "total_safety_stock_value": round(total_ss_value, 2),
    }
