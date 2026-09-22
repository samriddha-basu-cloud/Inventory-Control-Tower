"""Execution connectors. Only MockExecutionConnector ships with ICT.

The mock *simulates* PO creation, STO creation, transfers and expedites and returns synthetic document numbers
prefixed MOCK-. Its results are always flagged is_mock=True and are never presented as real ERP postings. Optionally
(`execution.apply_to_local_data`) it mirrors the effect into ICT's own tables tagged source_system='ICT_MOCK' so the
control tower can demonstrate the closed loop.
"""
from __future__ import annotations

import itertools
import uuid
from datetime import timedelta

from ..extensions import db
from .base import ExecResult, ExecutionConnector

_counter = itertools.count(1)


class MockExecutionConnector(ExecutionConnector):
    name = "MockExecutionConnector"
    is_mock = True
    supported = ("CREATE_PO", "EXPEDITE_PO", "TRANSFER_STOCK", "CHANGE_QUANTITY", "CHANGE_DATE", "ALLOCATE_STOCK", "DEALLOCATE_STOCK", "REPLENISH",
                 "RELEASE_STOCK", "QUARANTINE", "REVIEW_EXPIRY", "REVIEW_OBSOLESCENCE", "CHANGE_SAFETY_STOCK", "CHANGE_REORDER_POINT")

    PREFIX = {"CREATE_PO": "PO", "REPLENISH": "PO", "EXPEDITE_PO": "EXP", "TRANSFER_STOCK": "STO", "CHANGE_QUANTITY": "CHG", "CHANGE_DATE": "CHG",
              "ALLOCATE_STOCK": "ALC", "DEALLOCATE_STOCK": "ALC", "RELEASE_STOCK": "QM", "QUARANTINE": "QM", "REVIEW_EXPIRY": "TSK",
              "REVIEW_OBSOLESCENCE": "TSK", "CHANGE_SAFETY_STOCK": "POL", "CHANGE_REORDER_POINT": "POL"}

    def execute(self, action_type: str, payload: dict, dry_run: bool = True) -> ExecResult:
        if action_type not in self.supported:
            return ExecResult(False, None, f"{action_type} is not supported by {self.name}", payload, {}, True)
        ref = f"MOCK-{self.PREFIX.get(action_type, 'DOC')}-{next(_counter):06d}-{uuid.uuid4().hex[:4].upper()}"
        resp = {"status": "SIMULATED", "document": ref, "note": "SIMULATION - no transaction was posted to any ERP/WMS/TMS.",
                "action_type": action_type, "applied_locally": False}
        return ExecResult(True, ref, "Simulated by mock connector (no real system was changed).", payload, resp, True)

    def verify(self, result: ExecResult) -> dict:
        ok = bool(result.ok and result.external_ref and result.external_ref.startswith("MOCK-"))
        return {"verified": ok, "method": "mock echo check", "detail": "Mock document reference present." if ok else "No mock reference returned.", "is_mock": True}

    # ---- optional mirroring into ICT's own tables (demo closed loop) ------------------------------------------------
    def apply_locally(self, action, ref: str) -> dict:
        """Create ICT-native records tagged ICT_MOCK. Never touches inventory balances except through the ledger."""
        from ..models import PurchaseOrder, PurchaseOrderLine, ReplenishmentPolicy, SafetyStockPolicy, TransferOrder
        from ..services import settings_service as S
        today = S.today()
        t = action.action_type
        p = action.payload or {}
        if t in ("CREATE_PO", "REPLENISH") and action.supplier_id and action.location_id:
            lt = int(p.get("arrival_days") or 7)
            po = PurchaseOrder(po_number=ref, supplier_id=action.supplier_id, dest_location_id=action.location_id, order_date=today,
                               promised_date=today + timedelta(days=lt), eta_date=today + timedelta(days=lt), status="OPEN", source_system="ICT_MOCK")
            db.session.add(po)
            db.session.flush()
            db.session.add(PurchaseOrderLine(po_id=po.id, line_no=1, item_id=action.item_id, qty_ordered=action.qty or 0, qty_received=0.0, qty_in_transit=0.0,
                                             promised_date=po.promised_date, eta_date=po.eta_date, source_system="ICT_MOCK"))
            return {"created": "PurchaseOrder", "number": ref}
        if t == "TRANSFER_STOCK" and action.to_location_id is not None or (t == "TRANSFER_STOCK" and action.location_id):
            dest = action.to_location_id or action.location_id
            src = p.get("from_location_id") or action.location_id
            days = int(p.get("arrival_days") or 3)
            db.session.add(TransferOrder(to_number=ref, item_id=action.item_id, from_location_id=src, to_location_id=dest, qty=action.qty or 0,
                                         ship_date=today, eta_date=today + timedelta(days=days), promised_date=today + timedelta(days=days), status="OPEN",
                                         source_system="ICT_MOCK"))
            return {"created": "TransferOrder", "number": ref}
        if t == "CHANGE_SAFETY_STOCK":
            db.session.add(SafetyStockPolicy(scope_level="SKU_LOCATION", scope_key=f"{action.item.sku}|{action.location.code}", params=p.get("params", {}),
                                             notes=f"Set by action {action.action_no}", source_system="ICT_MOCK"))
            return {"created": "SafetyStockPolicy"}
        if t == "CHANGE_REORDER_POINT":
            db.session.add(ReplenishmentPolicy(scope_level="SKU_LOCATION", scope_key=f"{action.item.sku}|{action.location.code}", params=p.get("params", {}),
                                               notes=f"Set by action {action.action_no}", source_system="ICT_MOCK"))
            return {"created": "ReplenishmentPolicy"}
        return {}


CONNECTORS = {"mock": MockExecutionConnector()}
