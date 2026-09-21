"""
Allocation Engine (sections 45-46) — when supply is constrained across
competing demands, decide who gets what.
"""


def fifo_allocate(demands, available_supply):
    """demands: list of dict(id, quantity, order_date). Earliest order_date first."""
    ordered = sorted(demands, key=lambda d: d["order_date"])
    return _sequential_allocate(ordered, available_supply, rule="fifo")


def priority_allocate(demands, available_supply, priority_key="priority_weight"):
    """Higher priority_weight served first."""
    ordered = sorted(demands, key=lambda d: d.get(priority_key, 1.0), reverse=True)
    return _sequential_allocate(ordered, available_supply, rule="priority")


def proportional_allocate(demands, available_supply):
    """Fair-share: every demand gets the same % of what it asked for."""
    total_demand = sum(d["quantity"] for d in demands)
    results = []
    if total_demand <= 0:
        return {"rule": "proportional", "allocations": [], "total_demand": 0,
                "total_allocated": 0, "total_unfulfilled": 0}
    ratio = min(1.0, available_supply / total_demand)
    total_allocated = 0.0
    for d in demands:
        alloc = round(d["quantity"] * ratio, 2)
        total_allocated += alloc
        results.append({**d, "allocated_quantity": alloc,
                         "unfulfilled_quantity": round(d["quantity"] - alloc, 2)})
    return {
        "rule": "proportional",
        "fill_ratio_pct": round(ratio * 100, 1),
        "allocations": results,
        "total_demand": round(total_demand, 2),
        "total_allocated": round(total_allocated, 2),
        "total_unfulfilled": round(total_demand - total_allocated, 2),
    }


def margin_allocate(demands, available_supply, margin_key="margin_per_unit"):
    ordered = sorted(demands, key=lambda d: d.get(margin_key, 0.0), reverse=True)
    return _sequential_allocate(ordered, available_supply, rule="margin")


def _sequential_allocate(ordered_demands, available_supply, rule):
    remaining = available_supply
    results = []
    total_demand = 0.0
    total_allocated = 0.0
    for d in ordered_demands:
        qty = d["quantity"]
        total_demand += qty
        alloc = min(qty, max(remaining, 0))
        remaining -= alloc
        total_allocated += alloc
        results.append({**d, "allocated_quantity": round(alloc, 2),
                         "unfulfilled_quantity": round(qty - alloc, 2)})
    return {
        "rule": rule,
        "allocations": results,
        "total_demand": round(total_demand, 2),
        "total_allocated": round(total_allocated, 2),
        "total_unfulfilled": round(total_demand - total_allocated, 2),
        "supply_remaining": round(max(remaining, 0), 2),
    }


ALLOCATION_RULES = {
    "fifo": fifo_allocate,
    "priority_customer": priority_allocate,
    "proportional": proportional_allocate,
    "fair_share": proportional_allocate,
    "margin": margin_allocate,
}
