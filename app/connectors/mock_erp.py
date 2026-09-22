"""Mock ERP / WMS / TMS adapters for demonstration.

They emit records in SAP-, WMS- and TMS-style *native* field names derived from ICT's own demo data, so the ingestion
mapping path (native → canonical) can be exercised end-to-end. Every payload is stamped MOCK - this is not a live system.
"""
from __future__ import annotations

from ..models import InventoryBalance, Item, Location, PurchaseOrder, Shipment
from .base import ERPConnector, SourceConnector, TMSConnector, WMSConnector


class MockERPConnector(ERPConnector):
    name = "Mock ERP (SAP-style)"
    description = "Synthetic SAP-flavoured extract (MATNR/WERKS/LABST/EBELN). Demo only."

    def fetch(self, entity: str, since=None) -> list[dict]:
        if entity == "inventory":
            out = []
            for b in InventoryBalance.query.filter(InventoryBalance.state == "UNRESTRICTED").limit(400).all():
                out.append({"MATNR": b.item.sku, "WERKS": b.location.code, "LABST": b.quantity, "MEINS": "EA", "_mock": True})
            return out
        if entity == "purchase_orders":
            return [{"EBELN": po.po_number, "LIFNR": po.supplier.code if po.supplier else None, "EINDT": str(po.eta_date), "_mock": True}
                    for po in PurchaseOrder.query.limit(200).all()]
        raise ValueError(f"Mock ERP does not expose '{entity}'")

    def mapping(self, entity: str) -> dict[str, str]:
        return {"inventory": {"MATNR": "sku", "WERKS": "location", "LABST": "qty", "MEINS": "uom"},
                "purchase_orders": {"EBELN": "po_number", "LIFNR": "supplier", "EINDT": "eta_date"}}[entity]


class MockWMSConnector(WMSConnector):
    name = "Mock WMS"
    description = "Synthetic bin-level stock feed (SKU, LOC, ONHAND, HOLD). Demo only."

    def fetch(self, entity: str, since=None) -> list[dict]:
        if entity != "inventory":
            raise ValueError("Mock WMS exposes 'inventory' only")
        return [{"SKU": b.item.sku, "LOC": b.location.code, "ONHAND": b.quantity, "HOLD": b.state != "UNRESTRICTED", "_mock": True}
                for b in InventoryBalance.query.limit(400).all()]

    def mapping(self, entity: str) -> dict[str, str]:
        return {"SKU": "sku", "LOC": "location", "ONHAND": "qty"}


class MockTMSConnector(TMSConnector):
    name = "Mock TMS"
    description = "Synthetic shipment milestone feed (SHIPMENT_ID, ETA, STATUS). Demo only."

    def fetch(self, entity: str, since=None) -> list[dict]:
        if entity != "shipments":
            raise ValueError("Mock TMS exposes 'shipments' only")
        return [{"SHIPMENT_ID": s.shipment_no, "ETA": str(s.eta_date), "STATUS": s.status, "LANE": s.lane, "_mock": True} for s in Shipment.query.limit(200).all()]

    def mapping(self, entity: str) -> dict[str, str]:
        return {"SHIPMENT_ID": "shipment_no", "ETA": "eta_date", "STATUS": "status", "LANE": "lane"}


MOCK_SOURCES: dict[str, SourceConnector] = {"mock_erp": MockERPConnector(), "mock_wms": MockWMSConnector(), "mock_tms": MockTMSConnector()}
