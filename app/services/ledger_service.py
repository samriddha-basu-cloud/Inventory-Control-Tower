"""Event-aware, append-only inventory ledger.

Every stock movement is an InventoryTransaction; balances are updated in the same DB transaction and each
transaction stores the (item, location) on-hand *before* and *after*, forming a verifiable chain:

    Opening balance (period start)  +  Σ transactions  =  Closing balance

History is never edited: a correction is a new reversing transaction (`reverse()`), linked by reversal_of_id.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from ..extensions import db
from ..models import InventoryBalance, InventoryTransaction, Item, ItemUom, Location, Lot
from ..models.base import utcnow
from ..utils import uom as uomlib
from . import settings_service as S

TXN_TYPES = ["OPENING", "RECEIPT", "ISSUE", "TRANSFER", "ADJUSTMENT", "ALLOCATION", "DEALLOCATION", "SHIPMENT", "RETURN",
             "PROD_CONSUMPTION", "PROD_COMPLETION", "QUARANTINE", "RELEASE", "SCRAP", "CYCLE_COUNT"]
U = "UNRESTRICTED"


class LedgerError(ValueError):
    pass


class NegativeInventoryError(LedgerError):
    pass


class DuplicateTransactionError(LedgerError):
    def __init__(self, txn_id, existing=None):
        super().__init__(f"Duplicate transaction id '{txn_id}' - already posted.")
        self.existing = existing


def effects(txn_type: str, loc: int, to_loc: int | None, state_from: str | None, state_to: str | None,
            direction: str | None, qty: float) -> list[tuple[int, str, float]]:
    """(location, state, delta) tuples for a movement. Pure function shared by post() and bulk loaders."""
    sf, st = state_from or U, state_to or U
    if txn_type in ("OPENING", "RECEIPT", "PROD_COMPLETION"):
        return [(loc, st, qty)]
    if txn_type == "RETURN":
        return [(loc, state_to or "RETURNED", qty)]
    if txn_type in ("ISSUE", "SHIPMENT", "PROD_CONSUMPTION"):
        return [(loc, sf, -qty)]
    if txn_type == "SCRAP":
        return [(loc, sf, -qty), (loc, "SCRAP", qty)]
    if txn_type == "TRANSFER":
        if not to_loc:
            raise LedgerError("TRANSFER requires to_location")
        return [(loc, sf, -qty), (to_loc, state_to or sf, qty)]
    if txn_type == "QUARANTINE":
        return [(loc, sf, -qty), (loc, state_to or "QUARANTINED", qty)]
    if txn_type == "RELEASE":
        return [(loc, state_from or "QUARANTINED", -qty), (loc, state_to or U, qty)]
    if txn_type in ("ADJUSTMENT", "CYCLE_COUNT"):
        if direction == "OUT":
            return [(loc, sf, -qty)]
        return [(loc, st, qty)]
    if txn_type in ("ALLOCATION", "DEALLOCATION"):
        return []
    raise LedgerError(f"Unknown transaction type '{txn_type}'")


def _on_hand_states() -> set[str]:
    return {s["code"] for s in S.state_defs() if s["counts_on_hand"]}


def on_hand(item_id: int, location_id: int) -> float:
    states = _on_hand_states()
    rows = InventoryBalance.query.filter_by(item_id=item_id, location_id=location_id).all()
    return sum(r.quantity for r in rows if r.state in states)


def _balance(item_id, loc_id, lot_id, state, create=True) -> InventoryBalance | None:
    b = InventoryBalance.query.filter_by(item_id=item_id, location_id=loc_id, lot_id=lot_id, state=state).first()
    if not b and create:
        b = InventoryBalance(item_id=item_id, location_id=loc_id, lot_id=lot_id, state=state, quantity=0.0,
                             uom=Item.query.get(item_id).uom)
        db.session.add(b)
        db.session.flush()
    return b


def to_base_qty(item: Item, qty: float, uom: str | None) -> float:
    if not uom or uomlib.norm(uom) == uomlib.norm(item.uom):
        return float(qty)
    factors = {(r.from_uom, r.to_uom): r.factor for r in ItemUom.query.filter_by(item_id=item.id).all()}
    return uomlib.convert(qty, uom, item.uom, factors)


def post(txn_type: str, item_id: int, location_id: int, quantity: float, *, lot_id: int | None = None,
         to_location_id: int | None = None, uom: str | None = None, state_from: str | None = None,
         state_to: str | None = None, direction: str | None = None, reason: str | None = None,
         ref_type: str | None = None, ref_id: str | None = None, txn_id: str | None = None,
         occurred_at: datetime | None = None, customer_id: int | None = None, supplier_id: int | None = None,
         actor: str = "system", allow_negative: bool = False, source_system: str = "ICT") -> InventoryTransaction:
    if txn_type not in TXN_TYPES:
        raise LedgerError(f"Unknown transaction type '{txn_type}'")
    item = Item.query.get(item_id)
    if not item:
        raise LedgerError("Missing SKU: item not found.")
    if not Location.query.get(location_id):
        raise LedgerError("Missing location.")
    if quantity is None or quantity < 0:
        raise LedgerError("Quantity must be a non-negative magnitude; use direction/type for sign.")
    qty = to_base_qty(item, quantity, uom)
    txn_id = txn_id or uuid.uuid4().hex
    existing = InventoryTransaction.query.filter_by(txn_id=txn_id).first()
    if existing:
        raise DuplicateTransactionError(txn_id, existing)
    if item.lot_tracked and lot_id is None and txn_type not in ("ALLOCATION", "DEALLOCATION", "ADJUSTMENT", "CYCLE_COUNT"):
        raise LedgerError(f"{item.sku} is lot-tracked: a lot is required.")
    if txn_type in ("RECEIPT", "PROD_COMPLETION") and lot_id:
        lot = Lot.query.get(lot_id)
        if lot and lot.quality_status != "RELEASED" and not state_to:
            state_to = "QUARANTINED" if lot.quality_status == "QUARANTINE" else "BLOCKED"
    before = on_hand(item_id, location_id)
    eff = effects(txn_type, location_id, to_location_id, state_from, state_to, direction, qty)
    # negative-inventory guard (checked before any write)
    for loc, state, delta in eff:
        if delta < 0 and not allow_negative:
            b = _balance(item_id, loc, lot_id, state, create=False)
            have = b.quantity if b else 0.0
            if have + delta < -1e-9:
                raise NegativeInventoryError(
                    f"Insufficient stock: {item.sku} at location {loc} state {state} lot {lot_id}: "
                    f"have {have:,.2f}, need {qty:,.2f}.")
    now = occurred_at or S.now()
    for loc, state, delta in eff:
        b = _balance(item_id, loc, lot_id, state)
        b.quantity = round(b.quantity + delta, 6)
        b.last_movement_at = now
        if delta > 0 and txn_type in ("RECEIPT", "PROD_COMPLETION", "OPENING", "RETURN") or (delta > 0 and txn_type == "TRANSFER" and loc == to_location_id):
            b.last_receipt_at = now
    db.session.flush()
    after = on_hand(item_id, location_id)
    t = InventoryTransaction(txn_id=txn_id, txn_type=txn_type, item_id=item_id, location_id=location_id,
                             to_location_id=to_location_id, lot_id=lot_id, quantity=qty, uom=item.uom,
                             state_from=state_from, state_to=state_to, on_hand_before=before, on_hand_after=after,
                             occurred_at=now, ref_type=ref_type, ref_id=ref_id, customer_id=customer_id,
                             supplier_id=supplier_id, reason=reason, actor=actor, source_system=source_system)
    db.session.add(t)
    db.session.flush()
    S.bump_version()
    return t


def reverse(txn: InventoryTransaction, reason: str, actor: str = "system") -> InventoryTransaction:
    """Post an opposite transaction; the original is left untouched."""
    if InventoryTransaction.query.filter_by(reversal_of_id=txn.id).first():
        raise LedgerError("Transaction already reversed.")
    inv = {"RECEIPT": ("ISSUE", {}), "PROD_COMPLETION": ("PROD_CONSUMPTION", {}), "RETURN": ("ISSUE", {"state_from": "RETURNED"}),
           "ISSUE": ("RECEIPT", {}), "SHIPMENT": ("RECEIPT", {}), "PROD_CONSUMPTION": ("RECEIPT", {}),
           "OPENING": ("ISSUE", {})}
    kw = {}
    if txn.txn_type in inv:
        typ, kw = inv[txn.txn_type]
        if txn.txn_type in ("RECEIPT", "PROD_COMPLETION", "OPENING"):
            kw = {**kw, "state_from": txn.state_to or U}
        if txn.txn_type in ("ISSUE", "SHIPMENT", "PROD_CONSUMPTION"):
            kw = {**kw, "state_to": txn.state_from or U}
        new = post(typ, txn.item_id, txn.location_id, txn.quantity, lot_id=txn.lot_id, reason=f"REVERSAL: {reason}",
                   ref_type="REVERSAL", ref_id=str(txn.id), actor=actor, **kw)
    elif txn.txn_type == "TRANSFER":
        new = post("TRANSFER", txn.item_id, txn.to_location_id, txn.quantity, to_location_id=txn.location_id,
                   lot_id=txn.lot_id, state_from=txn.state_to or txn.state_from, state_to=txn.state_from,
                   reason=f"REVERSAL: {reason}", ref_type="REVERSAL", ref_id=str(txn.id), actor=actor)
    elif txn.txn_type in ("ADJUSTMENT", "CYCLE_COUNT"):
        new = post("ADJUSTMENT", txn.item_id, txn.location_id, txn.quantity, lot_id=txn.lot_id,
                   direction="OUT" if (txn.on_hand_after or 0) >= (txn.on_hand_before or 0) else "IN",
                   state_from=txn.state_to, state_to=txn.state_from, reason=f"REVERSAL: {reason}", ref_type="REVERSAL",
                   ref_id=str(txn.id), actor=actor)
    elif txn.txn_type in ("QUARANTINE", "RELEASE", "SCRAP"):
        new = post("ADJUSTMENT", txn.item_id, txn.location_id, txn.quantity, lot_id=txn.lot_id,
                   direction="IN", state_to=txn.state_from or U, reason=f"REVERSAL: {reason}", ref_type="REVERSAL",
                   ref_id=str(txn.id), actor=actor)
        post("ADJUSTMENT", txn.item_id, txn.location_id, txn.quantity, lot_id=txn.lot_id, direction="OUT",
             state_from=txn.state_to or "QUARANTINED", reason=f"REVERSAL: {reason}", ref_type="REVERSAL",
             ref_id=str(txn.id), actor=actor)
    else:
        raise LedgerError(f"{txn.txn_type} cannot be reversed automatically.")
    new.reversal_of_id = txn.id
    return new


def cycle_count(item_id: int, location_id: int, counted_qty: float, *, lot_id=None, state: str = U, actor="system",
                reason="Cycle count") -> InventoryTransaction | None:
    b = _balance(item_id, location_id, lot_id, state, create=False)
    system = b.quantity if b else 0.0
    delta = counted_qty - system
    if abs(delta) < 1e-9:
        return None
    return post("CYCLE_COUNT", item_id, location_id, abs(delta), lot_id=lot_id, direction="IN" if delta > 0 else "OUT",
                state_from=state, state_to=state, reason=f"{reason}: system {system:,.2f} → counted {counted_qty:,.2f}", actor=actor)


def _delta_at(t: InventoryTransaction, location_id: int, states: set[str]) -> float:
    direction = "OUT" if (t.txn_type in ("ADJUSTMENT", "CYCLE_COUNT") and (t.on_hand_after or 0) < (t.on_hand_before or 0)) else "IN"
    eff = effects(t.txn_type, t.location_id, t.to_location_id, t.state_from, t.state_to, direction, t.quantity)
    return sum(d for (l, s, d) in eff if l == location_id and s in states)


def ledger_report(item_id: int, location_id: int, start: date | None = None, end: date | None = None) -> dict:
    """Opening balance, transactions, closing balance for one item at one location (on-hand states only)."""
    rows = InventoryTransaction.query.filter(
        (InventoryTransaction.location_id == location_id) | (InventoryTransaction.to_location_id == location_id),
        InventoryTransaction.item_id == item_id).order_by(InventoryTransaction.occurred_at, InventoryTransaction.id).all()
    states = _on_hand_states()
    running, opening, lines, breaks = 0.0, None, [], []
    for t in rows:
        delta = _delta_at(t, location_id, states)
        if t.location_id == location_id and t.on_hand_before is not None and abs(t.on_hand_before - running) > 1e-6:
            breaks.append(t.id)
        d0 = t.occurred_at.date()
        if start and d0 < start:
            running += delta
            continue
        if opening is None:
            opening = running
        if end and d0 > end:
            running += delta
            continue
        running += delta
        lines.append({"txn": t, "delta": delta, "running": running})
    if opening is None:
        opening = running if start else 0.0
    closing = lines[-1]["running"] if lines else opening
    return {"opening": opening, "lines": lines, "closing": closing, "chain_breaks": breaks,
            "current_on_hand": on_hand(item_id, location_id)}
