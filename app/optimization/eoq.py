"""Economic Order Quantity + lot-size constraint reconciliation (sections 34, 37)."""
import math


def eoq(annual_demand, ordering_cost, annual_holding_cost_per_unit):
    """EOQ = sqrt(2*D*S / H)."""
    if annual_holding_cost_per_unit <= 0 or annual_demand <= 0:
        return {
            "formula": "EOQ = sqrt(2*D*S / H)",
            "inputs": {"annual_demand": annual_demand, "ordering_cost": ordering_cost,
                       "annual_holding_cost_per_unit": annual_holding_cost_per_unit},
            "eoq": 0.0,
        }
    raw = math.sqrt((2 * annual_demand * ordering_cost) / annual_holding_cost_per_unit)
    return {
        "formula": "EOQ = sqrt(2*D*S / H)",
        "inputs": {"annual_demand": annual_demand, "ordering_cost": ordering_cost,
                   "annual_holding_cost_per_unit": annual_holding_cost_per_unit},
        "eoq": round(raw, 2),
    }


def apply_lot_size_constraints(raw_quantity, moq=0.0, order_multiple=1.0, capacity_constraint=None):
    """
    Reconcile a theoretical order quantity (EOQ, or any recommended qty)
    against real-world constraints. Documents every step so the UI can show
    "why 2,500 and not 2,370".
    """
    steps = [f"Raw recommended quantity: {raw_quantity:.2f}"]
    qty = max(raw_quantity, 0)

    if moq and qty < moq:
        steps.append(f"Below supplier MOQ ({moq}) -> raised to MOQ")
        qty = moq

    order_multiple = order_multiple or 1.0
    if order_multiple > 1:
        rounded = math.ceil(qty / order_multiple) * order_multiple
        if rounded != qty:
            steps.append(f"Rounded up to order multiple of {order_multiple} -> {rounded}")
        qty = rounded

    capped = False
    if capacity_constraint is not None and qty > capacity_constraint:
        steps.append(f"Exceeds capacity constraint ({capacity_constraint}) -> capped")
        qty = capacity_constraint
        capped = True

    return {
        "raw_quantity": round(raw_quantity, 2),
        "moq": moq,
        "order_multiple": order_multiple,
        "capacity_constraint": capacity_constraint,
        "final_quantity": round(qty, 2),
        "capacity_capped": capped,
        "explanation_steps": steps,
    }
