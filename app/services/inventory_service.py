"""Canonical inventory state model and the explicit availability / position formulas.

Physical stock is partitioned into mutually-exclusive states (InventoryBalance.state). Claims and pipeline are
kept in separate tables, so a unit can never be counted in two buckets.

    On Hand (physical)        = Σ qty in states flagged counts_on_hand            (UNRESTRICTED, QUARANTINED, BLOCKED, RETURNED, ...)
    Usable On Hand            = Σ qty in states flagged allocatable               (UNRESTRICTED)
    Available Inventory       = Usable On Hand − Allocated − Committed − Reserved
    Net Available Inventory   = Available Inventory − Safety Stock                (free stock beyond the buffer)
    Inventory Position        = Usable On Hand + In-Transit + On-Order
                                − Allocated − Committed − Reserved − Backorders
    Projected Available Inv.  = time-phased (see projection_service): opening + receipts − demand ... per period

Allocated/Committed/Reserved are claims *against* usable stock; On Hand is never reduced by them.
"""
from __future__ import annotations

from typing import Iterable


def compute_position(stock_by_state: dict[str, float], states: Iterable[dict], *, allocated: float = 0.0,
                     committed: float = 0.0, reserved: float = 0.0, in_transit: float = 0.0, on_order: float = 0.0,
                     backorder: float = 0.0, safety_stock: float = 0.0, include_in_transit: bool = True) -> dict:
    states = list(states)
    on_hand_states = {s["code"] for s in states if s["counts_on_hand"]}
    usable_states = {s["code"] for s in states if s["allocatable"]}
    on_hand = sum(q for s, q in stock_by_state.items() if s in on_hand_states)
    usable = sum(q for s, q in stock_by_state.items() if s in usable_states)
    claims = allocated + committed + reserved
    available = usable - claims
    position = usable + (in_transit if include_in_transit else 0.0) + on_order - claims - backorder
    return {
        "on_hand": on_hand,
        "usable_on_hand": usable,
        "non_usable": on_hand - usable,
        "allocated": allocated, "committed": committed, "reserved": reserved,
        "in_transit": in_transit, "on_order": on_order, "backorder": backorder,
        "available": available,
        "net_available": available - safety_stock,
        "position": position,
        "over_allocated": available < -1e-9,
    }


POSITION_TOOLTIPS = {
    "on_hand": "Physical stock in the building (all counted states). Never reduced by allocations.",
    "available": "Usable stock minus allocated, committed and reserved claims - what can be promised today.",
    "position": "Available + in-transit + on-order − backorders. The quantity replenishment policies act on.",
    "projected": "Time-phased forecast of available stock per period using receipts and demand. The future view.",
    "net_available": "Available minus safety stock: stock genuinely free beyond the protective buffer.",
}
