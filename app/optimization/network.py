"""Network rebalancing engine (section 47) — match surplus nodes to deficit
nodes for the same SKU, minimizing transfer + expedite cost while resolving
shortage risk."""


def identify_surplus_deficit(node_positions, target_dos_days=14):
    """
    node_positions: list of dict(location, available, avg_demand_daily)
    Returns nodes classified as surplus/deficit vs a target days-of-supply.
    """
    classified = []
    for n in node_positions:
        dos = (n["available"] / n["avg_demand_daily"]) if n["avg_demand_daily"] > 0 else float("inf")
        if dos == float("inf"):
            status = "surplus" if n["available"] > 0 else "neutral"
        elif dos > target_dos_days * 1.5:
            status = "surplus"
        elif dos < target_dos_days * 0.5:
            status = "deficit"
        else:
            status = "neutral"
        classified.append({**n, "days_of_supply": None if dos == float("inf") else round(dos, 1),
                            "status": status})
    return classified


def recommend_transfers(classified_nodes, target_dos_days=14, transit_days_matrix=None,
                         cost_per_unit_matrix=None):
    """
    Greedily match the largest deficits with the largest surpluses for the
    same SKU. transit_days_matrix / cost_per_unit_matrix: dict[(src,dst)] -> value.
    """
    transit_days_matrix = transit_days_matrix or {}
    cost_per_unit_matrix = cost_per_unit_matrix or {}

    surplus = sorted(
        [n for n in classified_nodes if n["status"] == "surplus"],
        key=lambda n: n["available"] - n["avg_demand_daily"] * target_dos_days, reverse=True,
    )
    deficit = sorted(
        [n for n in classified_nodes if n["status"] == "deficit"],
        key=lambda n: n["avg_demand_daily"] * target_dos_days - n["available"], reverse=True,
    )

    surplus_pool = {s["location"]: s["available"] - s["avg_demand_daily"] * target_dos_days for s in surplus}
    recommendations = []
    for d in deficit:
        need = d["avg_demand_daily"] * target_dos_days - d["available"]
        for s_loc, s_avail in list(surplus_pool.items()):
            if need <= 0:
                break
            if s_avail <= 0:
                continue
            qty = min(need, s_avail)
            if qty <= 0:
                continue
            transit_days = transit_days_matrix.get((s_loc, d["location"]), 3)
            cost_per_unit = cost_per_unit_matrix.get((s_loc, d["location"]), 0.0)
            recommendations.append({
                "source": s_loc,
                "destination": d["location"],
                "quantity": round(qty, 2),
                "transit_days": transit_days,
                "transport_cost": round(qty * cost_per_unit, 2),
                "reason": (f"{d['location']} projected DOS is low while {s_loc} holds surplus "
                           f"beyond the {target_dos_days}-day target."),
            })
            surplus_pool[s_loc] -= qty
            need -= qty
    return recommendations
