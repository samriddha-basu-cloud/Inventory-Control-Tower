"""Connector interfaces.

Source connectors PULL data into the canonical model; execution connectors PUSH approved actions to a system of record.
ICT ships only mock implementations. Anything else is registered as a *planned* adapter that raises
ConnectorNotConfigured - ICT never pretends to be connected to a real ERP/WMS/TMS.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class ConnectorNotConfigured(RuntimeError):
    pass


class SourceConnector(ABC):
    name: str = "base"
    system: str = "GENERIC"
    is_mock: bool = True
    description: str = ""

    @abstractmethod
    def fetch(self, entity: str, since=None) -> list[dict]:
        """Return records in the source system's *native* field names."""

    @abstractmethod
    def mapping(self, entity: str) -> dict[str, str]:
        """native field → canonical field."""

    def health(self) -> dict:
        return {"connector": self.name, "mock": self.is_mock, "status": "MOCK" if self.is_mock else "UNKNOWN"}


@dataclass
class ExecResult:
    ok: bool
    external_ref: str | None
    message: str
    request: dict = field(default_factory=dict)
    response: dict = field(default_factory=dict)
    is_mock: bool = True


class ExecutionConnector(ABC):
    name: str = "base"
    is_mock: bool = True
    supported: tuple = ()

    @abstractmethod
    def execute(self, action_type: str, payload: dict, dry_run: bool = True) -> ExecResult: ...

    @abstractmethod
    def verify(self, result: ExecResult) -> dict: ...


class ERPConnector(SourceConnector):
    system = "ERP"


class WMSConnector(SourceConnector):
    system = "WMS"


class TMSConnector(SourceConnector):
    system = "TMS"


class ProcurementConnector(ExecutionConnector):
    """Future: PO create/change/expedite in SAP/Oracle/Dynamics/NetSuite procurement."""


class TransportationConnector(ExecutionConnector):
    """Future: booking/expedite via a TMS."""


class PlannedConnector(SourceConnector):
    """Registered-but-not-implemented adapter. Shows up in the catalogue as NOT CONFIGURED."""
    is_mock = False

    def __init__(self, name: str, system: str, description: str, required: list[str]):
        self.name, self.system, self.description, self.required = name, system, description, required

    def fetch(self, entity, since=None):
        raise ConnectorNotConfigured(f"{self.name} is not configured. Required: {', '.join(self.required)}. "
                                     "ICT does not simulate a live connection to this system.")

    def mapping(self, entity):
        raise ConnectorNotConfigured(f"{self.name} mapping is not available until the connector is implemented.")

    def health(self):
        return {"connector": self.name, "mock": False, "status": "NOT_CONFIGURED", "required": self.required}


PLANNED = [
    PlannedConnector("SAP S/4HANA", "ERP", "OData/BAPI extraction of MARD/EKKO/EKPO/VBAK", ["SAP host", "OAuth/technical user", "OData services"]),
    PlannedConnector("Oracle Fusion SCM", "ERP", "REST APIs for inventory, purchasing and order management", ["Oracle host", "OAuth client", "REST scopes"]),
    PlannedConnector("Microsoft Dynamics 365 SCM", "ERP", "Dataverse / OData entities", ["Tenant", "Azure AD app registration"]),
    PlannedConnector("NetSuite", "ERP", "SuiteTalk REST / SuiteQL", ["Account id", "Token-based auth"]),
    PlannedConnector("WMS (generic)", "WMS", "Stock-by-bin, receipts and picks via REST/DB view", ["Endpoint or read-only DB user"]),
    PlannedConnector("TMS (generic)", "TMS", "Shipment milestones and ETAs", ["Endpoint / API key"]),
    PlannedConnector("MES", "MES", "WIP, production completions and consumption", ["OPC-UA / REST endpoint"]),
    PlannedConnector("3PL portal", "3PL", "Stock-on-hand feeds from third-party warehouses", ["SFTP or API credentials"]),
    PlannedConnector("IoT sensors", "IOT", "Temperature/humidity and bin-level sensing", ["MQTT/Event Hub connection"]),
    PlannedConnector("Telematics", "TELEMATICS", "Live vehicle location and ETA", ["Provider API key"]),
]
