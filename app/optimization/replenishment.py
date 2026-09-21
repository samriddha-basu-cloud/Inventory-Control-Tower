"""Replenishment Control Center calculations (section 36)."""
from app.optimization.eoq import apply_lot_size_constraints


def min_max_recommendation(current_position, min_qty, max_qty, moq=0.0, order_multiple=1.0):
    if current_position >= min_qty:
        return {"policy": "min_max", "trigger": False, "recommended_quantity": 0.0,
                "reason": f"Inventory position {current_position} >= min {min_qty}"}
    raw_qty = max_qty - current_position
    constrained = apply_lot_size_constraints(raw_qty, moq, order_multiple)
    return {"policy": "min_max", "trigger": True, "raw_quantity": raw_qty,
            "recommended_quantity": constrained["final_quantity"], "constraint_detail": constrained}


def rop_eoq_recommendation(current_position, reorder_point, eoq_qty, moq=0.0, order_multiple=1.0):
    if current_position >= reorder_point:
        return {"policy": "rop_eoq", "trigger": False, "recommended_quantity": 0.0,
                "reason": f"Inventory position {current_position} >= ROP {reorder_point}"}
    constrained = apply_lot_size_constraints(eoq_qty, moq, order_multiple)
    return {"policy": "rop_eoq", "trigger": True, "raw_quantity": eoq_qty,
            "recommended_quantity": constrained["final_quantity"], "constraint_detail": constrained}


def order_up_to_recommendation(current_position, target_level, moq=0.0, order_multiple=1.0):
    """Periodic review / order-up-to-level (P-system)."""
    if current_position >= target_level:
        return {"policy": "order_up_to", "trigger": False, "recommended_quantity": 0.0,
                "reason": f"Inventory position {current_position} >= target {target_level}"}
    raw_qty = target_level - current_position
    constrained = apply_lot_size_constraints(raw_qty, moq, order_multiple)
    return {"policy": "order_up_to", "trigger": True, "raw_quantity": raw_qty,
            "recommended_quantity": constrained["final_quantity"], "constraint_detail": constrained}


def order_up_to_level(avg_demand_daily, review_period_days, lead_time_days, safety_stock):
    """Target level for order-up-to policy: covers review period + lead time + SS."""
    protection_period = review_period_days + lead_time_days
    target = avg_demand_daily * protection_period + safety_stock
    return {
        "formula": "S = D̄*(R+LT) + SS",
        "inputs": {"avg_demand_daily": avg_demand_daily, "review_period_days": review_period_days,
                   "lead_time_days": lead_time_days, "safety_stock": safety_stock},
        "order_up_to_level": round(target, 2),
    }


def kanban_recommendation(bin_size, num_bins_full, num_bins_total):
    """Two-bin/Kanban: trigger a refill order for one bin's worth when a bin empties."""
    consumed_bins = num_bins_total - num_bins_full
    if consumed_bins <= 0:
        return {"policy": "kanban", "trigger": False, "recommended_quantity": 0.0}
    return {"policy": "kanban", "trigger": True,
            "recommended_quantity": round(bin_size * consumed_bins, 2),
            "reason": f"{consumed_bins} of {num_bins_total} bins consumed"}
