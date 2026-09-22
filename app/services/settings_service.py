"""Configuration store.

Resolution order for every key:   explicit user setting (DB)  >  active industry profile override  >  DEFAULTS.
So switching industry mode re-configures behaviour without touching the engines, and an administrator's
explicit choice always wins.
"""
from __future__ import annotations

import copy
from datetime import date, datetime

from ..extensions import db
from ..models import IndustryProfile, Setting
from ..models.base import utcnow

DEFAULT_STATES = [
    # code, label, category, counts_on_hand, allocatable, counts_in_position, description
    ("UNRESTRICTED", "Available stock (released)", "PHYSICAL", True, True, True, "Usable, unblocked physical stock"),
    ("QUARANTINED", "Quarantined", "PHYSICAL", True, False, False, "Held for quality inspection - physically present, not usable"),
    ("BLOCKED", "Blocked", "PHYSICAL", True, False, False, "Blocked by planner/quality/ECO - not usable"),
    ("WIP", "Work in process", "PHYSICAL", False, False, False, "On the shop floor; not warehouse on-hand"),
    ("RETURNED", "Returned (pending disposition)", "PHYSICAL", True, False, False, "Customer returns awaiting inspection"),
    ("REPAIR", "In repair", "PHYSICAL", True, False, False, "Circular flow: repair"),
    ("REFURBISHMENT", "In refurbishment", "PHYSICAL", True, False, False, "Circular flow: refurbishment"),
    ("SCRAP", "Scrap", "PHYSICAL", False, False, False, "Written off / awaiting disposal"),
    ("OBSOLETE", "Obsolete (flagged)", "PHYSICAL", True, False, False, "Physically present, flagged obsolete"),
    ("ALLOCATED", "Allocated", "CLAIM", False, False, False, "Claim against usable stock for an order"),
    ("COMMITTED", "Committed", "CLAIM", False, False, False, "Hard commitment (picked / contractual)"),
    ("RESERVED", "Reserved", "CLAIM", False, False, False, "Manual/planner reservation"),
    ("IN_TRANSIT", "In transit", "PIPELINE", False, False, True, "Shipped, not yet received (PO/TO)"),
    ("ON_ORDER", "On order", "PIPELINE", False, False, True, "Ordered, not yet shipped"),
    ("BACKORDERED", "Backordered", "PIPELINE", False, False, True, "Customer demand not yet filled (reduces position)"),
    ("ON_HAND", "On hand", "DERIVED", False, False, False, "Sum of PHYSICAL states flagged counts_on_hand"),
    ("AVAILABLE", "Available", "DERIVED", False, False, False, "Allocatable stock minus claims"),
]

DEFAULTS: dict = {
    # --- engine -------------------------------------------------------------------------------------------------
    "engine.service_level": 0.95,
    "engine.ss_method": "combined",
    "engine.lt_basis": "observed_mean",      # static | observed_mean | p50 | p90
    "engine.use_observed_lt": True,
    "engine.min_lt_obs": 8,
    "engine.holding_rate": 0.22,             # annual: capital 12% + storage 5% + service 2% + risk 3%
    "engine.ordering_cost": 1500.0,
    "engine.review_days": 7,
    "engine.default_lt_days": 7,
    "engine.stats_window_weeks": 26,
    "engine.horizon_weeks": 13,
    "engine.forecast_precedence": ["ADJUSTED", "CONSENSUS", "BASELINE"],
    "engine.lost_sale_fraction": 0.6,        # share of unmet demand that is lost (rest backordered)
    "engine.default_margin_pct": 0.25,
    "engine.assumed_lt_cv": 0.0,             # LT std used when nothing observed (0 = none assumed; lowers confidence)
    "engine.position_includes_in_transit": True,
    "approval.no_self_approval": False,
    "alerts.suppress": [],
    "alerts.min_priority": 25.0,
    "optimization.carbon_price": 4.0,
    "sop.upside_pct": 15.0,
    "sop.downside_pct": 15.0,
    "thresholds.risk_exposure": {"medium": 50000, "high": 500000, "critical": 2000000},
    "transfer.policy": {"keep_safety_stock_at_donor": True, "max_transfer_days": 10, "min_transfer_qty": 1, "restricted_lanes": []},
    # --- excess / obsolescence ----------------------------------------------------------------------------------
    "excess.dos_threshold": 90,
    "excess.slow_days": 90,
    "excess.nonmoving_days": 180,
    "excess.obsolete_days": 365,
    "excess.obsolescence_prob": {"EXCESS": 0.03, "SLOW": 0.10, "NON_MOVING": 0.40, "OBSOLETE": 1.0},
    # --- thresholds ---------------------------------------------------------------------------------------------
    "thresholds.stockout": {"medium": 0.05, "high": 0.15, "critical": 0.35},
    "thresholds.expiry": {"near_days": 60, "critical_days": 30, "near_pct": 0.35},
    "thresholds.stale_data_factor": 1.0,
    # --- analytics ----------------------------------------------------------------------------------------------
    "aging.buckets": [30, 60, 90, 180, 365],
    "abc.thresholds": {"A": 0.80, "B": 0.95},          # cumulative annual consumption value cut-offs
    "xyz.thresholds": {"X": 0.5, "Y": 1.0},            # CV cut-offs (weekly demand)
    "fsn.thresholds": {"fast_turns": 6.0, "non_moving_days": 180},
    "hml.thresholds": {"H": 0.20, "M": 0.50},          # cumulative share of items by unit value (top 20% = H)
    # --- scoring weights ----------------------------------------------------------------------------------------
    "weights.priority": {"financial": 0.30, "service": 0.20, "production": 0.15, "customer": 0.10,
                         "severity": 0.15, "time": 0.10},
    "weights.health": {"availability": 0.20, "service": 0.15, "excess": 0.12, "obsolescence": 0.10,
                       "accuracy": 0.10, "forecast": 0.08, "stockout_risk": 0.10, "aging": 0.07, "lead_time": 0.08},
    "weights.supplier_risk": {"otif": 0.30, "lt_reliability": 0.20, "quality": 0.15, "concentration": 0.15,
                              "geo": 0.10, "expedite": 0.10},
    "weights.confidence": {"completeness": 0.25, "freshness": 0.20, "stability": 0.20, "model": 0.20, "constraints": 0.15},
    # --- optimisation -------------------------------------------------------------------------------------------
    "optimization.weights": {"stockout": 5.0, "holding": 1.0, "expedite": 1.5, "carbon": 0.2,
                             "working_capital": 0.3, "obsolescence": 0.5},
    "optimization.enforce_service_level": True,
    "optimization.budget": 5000000.0,
    "optimization.warehouse_capacity_m3": None,
    "optimization.truck_capacity_kg": 12000.0,
    # --- reconciliation -----------------------------------------------------------------------------------------
    "recon.rules": {"tolerance_pct": 0.02, "tolerance_abs": 2.0, "stale_hours": 24,
                    "checks": ["missing", "duplicate", "negative", "unit", "location", "timing"]},
    # --- autonomy / execution -----------------------------------------------------------------------------------
    "autonomy.default_level": 1,
    "execution.mode": "SIMULATION_ONLY",               # RECOMMENDATION | SIMULATION_ONLY | LIVE (needs real connector)
    "execution.apply_to_local_data": False,
    "approval.matrix": [
        {"max_cost": 50000, "role": "Inventory Planner"},
        {"max_cost": 500000, "role": "Supply Chain Manager"},
        {"max_cost": None, "role": "Executive"},
    ],
    # --- freight / carbon (configured factors; outputs are ESTIMATES) -------------------------------------------
    "carbon.factors": {"ROAD": 0.062, "RAIL": 0.022, "SEA": 0.016, "AIR": 0.602},   # kg CO2e per tonne-km
    "freight.rate_per_tkm": {"ROAD": 4.5, "RAIL": 2.2, "SEA": 1.4, "AIR": 30.0},     # currency per tonne-km
    "freight.min_charge": 2500.0,
    "freight.speed_kmpd": {"ROAD": 450.0, "RAIL": 350.0, "SEA": 500.0, "AIR": 4000.0},
    "freight.handling_days": {"ROAD": 0.5, "RAIL": 1.5, "SEA": 3.0, "AIR": 1.0},
    "freight.expedite_mode": {"ROAD": "AIR", "RAIL": "ROAD", "SEA": "AIR", "AIR": "AIR"},
    # --- industry / misc ----------------------------------------------------------------------------------------
    "industry.active": "GENERAL",
    "system.as_of": None,
    "system.data_version": 1,
    "system.demo_loaded": None,
    "system.last_detection": None,
    "notifications.enabled_channels": [],
}

_cache: dict = {"ver": None, "vals": {}, "profile": None}
_local_version = 0


def _db_version() -> int:
    row = Setting.query.filter_by(key="system.data_version").first()
    return int(row.value) if row and row.value is not None else 1


def _sync():
    """Invalidate the in-process cache when any worker bumped the data version."""
    ver = (_db_version(), _local_version)
    if _cache["ver"] != ver:
        rows = {s.key: s.value for s in Setting.query.all()}
        _cache.update(ver=ver, vals=rows, profile=None)
        active = rows.get("industry.active", DEFAULTS["industry.active"])
        prof = IndustryProfile.query.filter_by(code=active).first()
        _cache["profile"] = (prof.policies or {}) if prof else {}
    return _cache


def data_version() -> tuple:
    return _sync()["ver"]


def bump_version() -> None:
    """Call after any data/config write: invalidates settings + snapshot caches in every worker."""
    global _local_version
    _local_version += 1
    row = Setting.query.filter_by(key="system.data_version").first()
    if row:
        row.value = int(row.value or 1) + 1
    else:
        db.session.add(Setting(key="system.data_version", value=2))
    db.session.flush()
    _cache["ver"] = None


def get(key: str, default=None):
    c = _sync()
    if key in c["vals"]:
        return copy.deepcopy(c["vals"][key])
    if key in (c["profile"] or {}):
        return copy.deepcopy(c["profile"][key])
    if key in DEFAULTS:
        return copy.deepcopy(DEFAULTS[key])
    return default


def get_source(key: str) -> str:
    c = _sync()
    if key in c["vals"]:
        return "USER"
    if key in (c["profile"] or {}):
        return "INDUSTRY_PROFILE"
    return "DEFAULT"


def set_value(key: str, value, actor: str = "system", audit: bool = True) -> None:
    row = Setting.query.filter_by(key=key).first()
    old = row.value if row else DEFAULTS.get(key)
    if row:
        row.value = value
        row.updated_by = actor
        row.updated_at = utcnow()
    else:
        db.session.add(Setting(key=key, value=value, updated_by=actor))
    if audit and key != "system.data_version":
        from . import audit_service
        audit_service.log("CONFIG", "Setting", key, "setting_changed", {"old": old, "new": value}, actor=actor)
    bump_version()


def reset_value(key: str, actor: str = "system") -> None:
    Setting.query.filter_by(key=key).delete()
    from . import audit_service
    audit_service.log("CONFIG", "Setting", key, "setting_reset", {}, actor=actor)
    bump_version()


def today() -> date:
    v = get("system.as_of")
    if v:
        try:
            return date.fromisoformat(v)
        except ValueError:
            pass
    return date.today()


def now() -> datetime:
    v = get("system.as_of")
    if v:
        try:
            return datetime.combine(date.fromisoformat(v), datetime.now().time())
        except ValueError:
            pass
    return utcnow()


def active_industry() -> str:
    return get("industry.active")


def state_defs() -> list[dict]:
    from ..models import InventoryStateDef
    rows = InventoryStateDef.query.filter_by(enabled=True).all()
    if rows:
        return [dict(code=r.code, label=r.label, category=r.category, counts_on_hand=r.counts_on_hand,
                     allocatable=r.allocatable, counts_in_position=r.counts_in_position) for r in rows]
    return [dict(code=c, label=l, category=cat, counts_on_hand=oh, allocatable=al, counts_in_position=pos)
            for c, l, cat, oh, al, pos, _ in DEFAULT_STATES]
