"""Allocation Engine service — applies constrained-supply allocation rules to
real open sales-order demand (sections 45-46)."""
from app.models import SalesOrderLine, SalesOrder, Customer, Item
from app.services import inventory_service
from app.optimization.allocation import ALLOCATION_RULES


def allocate_item(item_id, location_id, rule="priority_customer"):
    item = Item.query.get(item_id)
    available = inventory_service.available_quantity(item_id, location_id)

    lines = (
        SalesOrderLine.query.join(SalesOrder)
        .filter(SalesOrderLine.item_id == item_id, SalesOrder.location_id == location_id,
                SalesOrder.status == "OPEN")
        .all()
    )
    demands = []
    for line in lines:
        remaining = line.quantity_ordered - line.quantity_allocated
        if remaining <= 0:
            continue
        customer = line.so.customer
        demands.append({
            "so_line_id": line.id, "so_number": line.so.so_number, "customer": customer.name,
            "quantity": remaining, "order_date": line.so.order_date,
            "priority_weight": customer.priority_weight, "margin_per_unit": item.unit_price - item.unit_cost,
        })

    fn = ALLOCATION_RULES.get(rule, ALLOCATION_RULES["priority_customer"])
    result = fn(demands, available) if demands else {
        "rule": rule, "allocations": [], "total_demand": 0, "total_allocated": 0, "total_unfulfilled": 0,
    }
    result["available_supply"] = round(available, 2)
    result["item"] = item.sku if item else None
    revenue_at_risk = result.get("total_unfulfilled", 0) * (item.unit_price if item else 0)
    result["revenue_at_risk"] = round(revenue_at_risk, 2)
    return result
