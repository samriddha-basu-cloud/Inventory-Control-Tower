"""Financial and sustainability impact engines (inventory value, carrying cost, working capital, lost sales, expedite,
obsolescence, markdown, carbon). Every figure is derived from configured rates; nothing is hard-coded per industry."""
from __future__ import annotations

from ..models import ReturnRecord, ReusableAsset, Shipment, TransferOrder
from . import carbon_service as carbon
from . import settings_service as S
from .snapshot import PairFilter, Snapshot


def overview(snap: Snapshot, f: PairFilter | None = None) -> dict:
    t = snap.totals(f)
    cfg = snap.cfg
    keys = snap.keys(f)
    markdown = 0.0
    obs_cost = 0.0
    stockout_cost = 0.0
    for k in keys:
        r, inp = snap.results[k], snap.inputs[k]
        md = inp.item.get("markdown_pct") or 0.0
        if md and (r.inv_class in ("EXCESS", "SLOW", "NON_MOVING") or r.expiry_status in ("NEAR", "CRITICAL")):
            markdown += (r.excess_value or r.on_hand_value * 0.3) * md
        obs_cost += r.obsolescence_exposure
        stockout_cost += r.lost_margin + r.production_risk
    ship_q = Shipment.query.filter(Shipment.expedited.is_(True))
    exp_cost = sum(carbon.freight_cost(s.weight_kg, s.distance_km, s.mode) - carbon.freight_cost(s.weight_kg, s.distance_km, "ROAD")
                   for s in ship_q.all())
    transfer_cost = 0.0
    for to in TransferOrder.query.filter(TransferOrder.status.in_(["IN_TRANSIT", "OPEN"])).all():
        it = snap.items.get(to.item_id)
        a, b = snap.locs.get(to.from_location_id), snap.locs.get(to.to_location_id)
        if it and a and b:
            from ..utils.geo import route_km
            transfer_cost += carbon.freight_cost(to.qty * it["weight_kg"], route_km(a["lat"], a["lon"], b["lat"], b["lon"]), to.mode)
    return {
        "inventory_value": t.get("on_hand_value", 0.0), "carrying_cost_year": t.get("carrying_cost_year", 0.0),
        "working_capital": t.get("on_hand_value", 0.0) + t.get("in_transit_value", 0.0),
        "excess_capital": t.get("excess_value", 0.0), "obsolescence_exposure": t.get("obsolescence_exposure", 0.0),
        "cost_of_stockout": stockout_cost, "lost_sales": t.get("lost_sales_value", 0.0), "expedite_cost": exp_cost,
        "transfer_cost": transfer_cost, "markdown_exposure": markdown, "obsolete_value": t.get("obsolete_value", 0.0),
        "at_risk_value": t.get("at_risk_value", 0.0), "holding_rate": cfg.holding_rate,
        "excess_carrying_cost": t.get("excess_value", 0.0) * cfg.holding_rate,
        "in_transit_value": t.get("in_transit_value", 0.0), "on_order_value": t.get("on_order_value", 0.0),
    }


def cost_compare(current: float, proposed: float) -> dict:
    return {"current": current, "proposed": proposed, "delta": proposed - current, "delta_pct": ((proposed - current) / current) if current else None}


def carbon_overview(snap: Snapshot) -> dict:
    """Pipeline (in-transit) freight CO2e, expedited-freight CO2e and circular volumes. All are estimates."""
    factors = S.get("carbon.factors")
    pipeline = exped = 0.0
    by_mode: dict[str, float] = {}
    for s in Shipment.query.filter(Shipment.status != "DELIVERED").all():
        e = carbon.emissions_kg(s.weight_kg, s.distance_km, s.mode, factors)
        pipeline += e
        by_mode[s.mode] = by_mode.get(s.mode, 0.0) + e
        if s.expedited:
            exped += e
    ret = sum(r.qty for r in ReturnRecord.query.all())
    assets = ReusableAsset.query.all()
    return {"pipeline_co2e_kg": pipeline, "expedited_co2e_kg": exped, "by_mode": by_mode, "return_units": ret,
            "assets_available": sum(a.qty_available for a in assets), "assets_total": sum(a.qty_available + a.qty_in_transit + a.qty_repair for a in assets),
            "factors": factors, "disclaimer": carbon.DISCLAIMER}
