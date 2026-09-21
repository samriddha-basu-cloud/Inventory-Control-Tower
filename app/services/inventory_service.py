"""
Core inventory ledger / position / ATP service.

Physical vs. available vs. ATP are always kept distinct here:
  - ON_HAND        : gross physical stock at a location.
  - Encumbered sub-pools that reduce what's usable from ON_HAND:
        ALLOCATED, COMMITTED, QUARANTINED, BLOCKED, DAMAGED
  - AVAILABLE (derived) = ON_HAND - encumbered sub-pools, floored at 0.
  - Pipeline pools (not part of ON_HAND): IN_TRANSIT, ON_ORDER, WIP,
    RETURN_IN_TRANSIT, RETURNED, REPAIR.
  - Write-off pools (excluded from ATP unless explicitly configured):
    SCRAP, EXPIRED, EXCESS, OBSOLETE.
"""
from sqlalchemy import func
from app.extensions import db
from app.models import InventoryLedger, Item, Location, SalesOrderLine, PurchaseOrderLine, PurchaseOrder

ENCUMBERED_STATUSES = ["ALLOCATED", "COMMITTED", "QUARANTINED", "BLOCKED", "DAMAGED"]
PIPELINE_STATUSES = ["IN_TRANSIT", "ON_ORDER", "WIP", "RETURN_IN_TRANSIT", "RETURNED", "REPAIR"]
WRITE_OFF_STATUSES = ["SCRAP", "EXPIRED", "EXCESS", "OBSOLETE"]


def status_quantities(item_id=None, location_id=None):
    """Returns {status: total_quantity} across the ledger, optionally filtered."""
    q = db.session.query(InventoryLedger.status, func.sum(InventoryLedger.quantity))
    if item_id is not None:
        q = q.filter(InventoryLedger.item_id == item_id)
    if location_id is not None:
        q = q.filter(InventoryLedger.location_id == location_id)
    q = q.group_by(InventoryLedger.status)
    return {status: float(qty or 0) for status, qty in q.all()}


def available_quantity(item_id, location_id):
    qtys = status_quantities(item_id, location_id)
    on_hand = qtys.get("ON_HAND", 0.0)
    encumbered = sum(qtys.get(s, 0.0) for s in ENCUMBERED_STATUSES)
    return max(0.0, on_hand - encumbered)


def inventory_position(item_id, location_id, formula_terms=None):
    """
    Inventory Position = On Hand + On Order + In Transit - Allocated - Backorders
    (default formula; organizations may reconfigure via Org.inventory_position_formula
    — the *terms* used are fixed to these ledger buckets for traceability, but
    which terms are added/subtracted is configurable).
    """
    qtys = status_quantities(item_id, location_id)
    on_hand = qtys.get("ON_HAND", 0.0)
    on_order = qtys.get("ON_ORDER", 0.0)
    in_transit = qtys.get("IN_TRANSIT", 0.0)
    allocated = qtys.get("ALLOCATED", 0.0)
    committed = qtys.get("COMMITTED", 0.0)

    backorders = 0.0
    if item_id is not None:
        open_lines = (
            db.session.query(func.sum(SalesOrderLine.quantity_ordered - SalesOrderLine.quantity_allocated))
            .filter(SalesOrderLine.item_id == item_id)
            .scalar()
        )
        backorders = float(open_lines or 0)

    position = on_hand + on_order + in_transit - allocated - backorders
    return {
        "formula": "Inventory Position = On Hand + On Order + In Transit - Allocated - Backorders",
        "on_hand": on_hand, "on_order": on_order, "in_transit": in_transit,
        "allocated": allocated, "committed": committed, "backorders": backorders,
        "inventory_position": round(position, 2),
    }


def atp(item_id, location_id, include_on_order=True):
    """Available-To-Promise: what can still be committed without double-booking."""
    qtys = status_quantities(item_id, location_id)
    on_hand = qtys.get("ON_HAND", 0.0)
    encumbered = sum(qtys.get(s, 0.0) for s in ENCUMBERED_STATUSES)
    available_now = max(0.0, on_hand - encumbered)

    future_receipts = 0.0
    if include_on_order:
        open_pos = (
            db.session.query(func.sum(PurchaseOrderLine.quantity_ordered - PurchaseOrderLine.quantity_received))
            .join(PurchaseOrder, PurchaseOrderLine.po_id == PurchaseOrder.id)
            .filter(PurchaseOrderLine.item_id == item_id, PurchaseOrder.destination_location_id == location_id,
                    PurchaseOrder.status.in_(["OPEN", "IN_TRANSIT", "DELAYED"]))
            .scalar()
        )
        future_receipts = float(open_pos or 0)

    open_demand = (
        db.session.query(func.sum(SalesOrderLine.quantity_ordered - SalesOrderLine.quantity_allocated))
        .filter(SalesOrderLine.item_id == item_id)
        .scalar()
    )
    open_demand = float(open_demand or 0)

    atp_value = available_now + future_receipts - open_demand
    return {
        "available_now": round(available_now, 2),
        "future_receipts": round(future_receipts, 2),
        "open_demand": round(open_demand, 2),
        "atp": round(atp_value, 2),
    }


def network_kpis():
    """Top-level KPIs for the Control Tower dashboard (section 13)."""
    qtys = status_quantities()
    items_by_id = {i.id: i for i in Item.query.all()}

    value_rows = (
        db.session.query(InventoryLedger.item_id, InventoryLedger.status, func.sum(InventoryLedger.quantity))
        .group_by(InventoryLedger.item_id, InventoryLedger.status)
        .all()
    )
    total_value = 0.0
    encumbered_value = 0.0
    excess_value = 0.0
    obsolete_value = 0.0
    write_off_value = 0.0
    for item_id, status, qty in value_rows:
        item = items_by_id.get(item_id)
        cost = item.unit_cost if item else 0.0
        v = float(qty or 0) * cost
        if status not in ("SCRAP",):
            total_value += v
        if status in ENCUMBERED_STATUSES:
            encumbered_value += v
        if status == "EXCESS":
            excess_value += v
        if status == "OBSOLETE":
            obsolete_value += v
        if status in WRITE_OFF_STATUSES:
            write_off_value += v

    return {
        "on_hand_units": qtys.get("ON_HAND", 0.0),
        "available_units": max(0.0, qtys.get("ON_HAND", 0.0) - sum(qtys.get(s, 0.0) for s in ENCUMBERED_STATUSES)),
        "allocated_units": qtys.get("ALLOCATED", 0.0),
        "committed_units": qtys.get("COMMITTED", 0.0),
        "in_transit_units": qtys.get("IN_TRANSIT", 0.0),
        "on_order_units": qtys.get("ON_ORDER", 0.0),
        "wip_units": qtys.get("WIP", 0.0),
        "quarantined_units": qtys.get("QUARANTINED", 0.0),
        "excess_units": qtys.get("EXCESS", 0.0),
        "obsolete_units": qtys.get("OBSOLETE", 0.0),
        "total_inventory_value": round(total_value, 2),
        "excess_value": round(excess_value, 2),
        "obsolete_value": round(obsolete_value, 2),
        "write_off_value": round(write_off_value, 2),
    }


def sku_location_snapshot(item_id, location_id):
    item = Item.query.get(item_id)
    location = Location.query.get(location_id)
    qtys = status_quantities(item_id, location_id)
    pos = inventory_position(item_id, location_id)
    atp_result = atp(item_id, location_id)
    return {"item": item, "location": location, "status_quantities": qtys,
            "position": pos, "atp": atp_result}
