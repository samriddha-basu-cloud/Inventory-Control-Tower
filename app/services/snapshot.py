"""Builds the in-memory operational snapshot from the database and runs the engine over every item-location pair.

The snapshot holds plain dicts / dataclasses only (no ORM objects), is cached per data-version, and is the
single source of truth for dashboards, alerts, APIs, scenarios and reports. The digital twin deep-copies its
PairInputs, so simulations can never touch production tables.
"""
from __future__ import annotations

import copy
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..extensions import db
from ..models import (Allocation, BomLine, Customer, Demand, Forecast, InventoryBalance, Item, ItemLocationSource,
                      ItemSupplier, LeadTimeObservation, Location, Lot, ProductionOrder, PurchaseOrder,
                      PurchaseOrderLine, ReplenishmentPolicy, SafetyStockPolicy, SalesOrder, SalesOrderLine, Shipment,
                      Supplier, SyncStatus, TransferOrder)
from ..rules.policies import PolicyIndex
from ..utils.geo import route_km
from . import abc_xyz_service as seg
from . import settings_service as S
from .engine import EngineConfig, PairInputs, PairResult, compute_pair, config_from_settings


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


# --------------------------------------------------------------------------------------------------- filters
@dataclass
class PairFilter:
    region: str = ""
    business_unit: str = ""
    industry: str = ""
    sku: str = ""
    family: str = ""
    location: str = ""
    supplier: str = ""
    customer: str = ""
    warehouse: str = ""
    date_from: str = ""
    date_to: str = ""

    KEYS = ("region", "business_unit", "industry", "sku", "family", "location", "supplier", "customer", "warehouse",
            "date_from", "date_to")

    @classmethod
    def from_mapping(cls, m) -> "PairFilter":
        return cls(**{k: (m.get(k) or "").strip() for k in cls.KEYS})

    def active(self) -> dict:
        return {k: getattr(self, k) for k in self.KEYS if getattr(self, k)}

    def is_empty(self) -> bool:
        return not self.active()


# --------------------------------------------------------------------------------------------------- snapshot
@dataclass
class Snapshot:
    cfg: EngineConfig
    today: date
    items: dict = field(default_factory=dict)
    locs: dict = field(default_factory=dict)
    suppliers: dict = field(default_factory=dict)
    customers: dict = field(default_factory=dict)
    inputs: dict = field(default_factory=dict)      # (item_id, loc_id) -> PairInputs
    results: dict = field(default_factory=dict)     # (item_id, loc_id) -> PairResult
    item_seg: dict = field(default_factory=dict)    # item_id -> segmentation dict
    freshness: dict = field(default_factory=dict)
    sync: list = field(default_factory=list)

    # ---- selection
    def keys(self, f: PairFilter | None = None) -> list[tuple]:
        if f is None or f.is_empty():
            return list(self.results.keys())
        out = []
        for k, inp in self.inputs.items():
            it, lc = inp.item, inp.loc
            if f.region and lc.get("region") != f.region:
                continue
            if f.business_unit and (it.get("business_unit") != f.business_unit and lc.get("business_unit") != f.business_unit):
                continue
            if f.industry and it.get("industry") != f.industry:
                continue
            if f.sku and f.sku.lower() not in it["sku"].lower():
                continue
            if f.family and it.get("family_code") != f.family:
                continue
            if f.location and lc.get("code") != f.location:
                continue
            if f.warehouse and lc.get("code") != f.warehouse:
                continue
            if f.supplier:
                sup = self.suppliers.get(inp.supplier_id, {})
                if sup.get("code") != f.supplier:
                    continue
            if f.customer:
                cust_ids = {o.get("customer_id") for o in inp.orders}
                if not any(self.customers.get(c, {}).get("code") == f.customer for c in cust_ids):
                    continue
            out.append(k)
        return out

    def rows(self, f: PairFilter | None = None):
        for k in self.keys(f):
            yield self.inputs[k], self.results[k]

    # ---- aggregation
    def totals(self, f: PairFilter | None = None) -> dict:
        t = defaultdict(float)
        for inp, r in self.rows(f):
            p = r.pos
            t["on_hand_units"] += p["on_hand"]
            t["on_hand_value"] += r.on_hand_value
            t["usable_value"] += r.usable_value
            t["available_value"] += p["available"] * r.unit_cost
            t["allocated_value"] += p["allocated"] * r.unit_cost
            t["committed_value"] += (p["committed"] + p["reserved"]) * r.unit_cost
            t["in_transit_value"] += r.in_transit_value
            t["on_order_value"] += r.on_order_value
            t["wip_value"] += r.wip_value
            t["quarantined_value"] += r.quarantined_value
            t["blocked_value"] += r.blocked_value
            t["excess_value"] += r.excess_value
            t["obsolete_value"] += r.obsolete_value
            t["at_risk_value"] += r.at_risk_value
            t["obsolescence_exposure"] += r.obsolescence_exposure
            t["carrying_cost_year"] += r.on_hand_value * self.cfg.holding_rate
            t["lost_sales_value"] += r.lost_sales_value
            t["lost_margin"] += r.lost_margin
            t["production_risk"] += r.production_risk
            t["pairs"] += 1
            if r.risk_level in ("HIGH", "CRITICAL"):
                t["at_risk_pairs"] += 1
            if r.risk_level == "CRITICAL":
                t["critical_pairs"] += 1
            t["stockout_prob_weighted"] += r.stockout_prob * max(r.d_mean * r.price, 1e-9)
            t["demand_value_daily"] += r.d_mean * r.price
            t["cogs_daily"] += r.d_mean * r.unit_cost
        for key in ("pairs", "at_risk_pairs", "critical_pairs", "on_hand_value", "available_value", "in_transit_value", "on_order_value", "excess_value", "obsolete_value", "at_risk_value"):
            t.setdefault(key, 0.0)          # templates and KPIs can rely on every headline key existing, even for an empty filter
        return dict(t)

    def item_agg(self, item_id: int) -> dict:
        rows = [self.results[k] for k in self.results if k[0] == item_id]
        return {"on_hand": sum(r.pos["on_hand"] for r in rows), "value": sum(r.on_hand_value for r in rows),
                "position": sum(r.pos["position"] for r in rows), "pairs": len(rows)}

    def keys_for_item(self, item_id: int) -> list[tuple]:
        return [k for k in self.results if k[0] == item_id]

    def keys_for_loc(self, loc_id: int) -> list[tuple]:
        return [k for k in self.results if k[1] == loc_id]


_CACHE: dict = {"ver": None, "snap": None}


def get_snapshot(force: bool = False) -> Snapshot:
    ver = S.data_version()
    if force or _CACHE["ver"] != ver or _CACHE["snap"] is None or _CACHE["snap"].today != S.today():
        _CACHE["snap"] = build_snapshot()
        _CACHE["ver"] = S.data_version()
    return _CACHE["snap"]


def clear_cache():
    _CACHE.update(ver=None, snap=None)


# --------------------------------------------------------------------------------------------------- builders
def _item_dict(i: Item, today: date) -> dict:
    return {
        "id": i.id, "sku": i.sku, "description": i.description, "family_code": i.family_code, "category": i.category,
        "industry": i.industry, "business_unit": i.business_unit, "item_type": i.item_type, "uom": i.uom,
        "unit_cost": i.unit_cost, "selling_price": i.selling_price, "moq": i.moq, "order_multiple": i.order_multiple,
        "criticality": i.criticality, "shelf_life_days": i.shelf_life_days, "weight_kg": i.weight_kg or 1.0,
        "volume_m3": i.volume_m3 or 0.001, "lot_tracked": bool(i.lot_tracked), "lifecycle_status": i.lifecycle_status,
        "eol_day": (i.eol_date - today).days if i.eol_date else None, "revision": i.revision,
        "superseded_by_sku": i.superseded_by_sku, "eco_day": (i.eco_effective_date - today).days if i.eco_effective_date else None,
        "eco_reworkable": bool(i.eco_reworkable), "target_service_level": i.target_service_level,
        "hazardous": bool(i.hazardous), "temp_requirement": i.temp_requirement, "ved": i.ved, "sde": i.sde,
        "line_stop_cost_per_unit": i.line_stop_cost_per_unit, "substitute_group": i.substitute_group,
        "markdown_pct": i.markdown_pct or 0.0, "temp": i.temp_requirement, "abc_master": i.abc_class, "xyz_master": i.xyz_class,
        "currency": i.currency, "status": i.status,
    }


def _loc_dict(l: Location) -> dict:
    return {"id": l.id, "code": l.code, "name": l.name, "loc_type": l.loc_type, "echelon": l.echelon, "parent_id": l.parent_id,
            "region": l.region, "country": l.country, "city": l.city, "lat": l.lat, "lon": l.lon, "industry": l.industry,
            "business_unit": l.business_unit, "capacity_units": l.capacity_units, "capacity_m3": l.capacity_m3,
            "is_3pl": bool(l.is_3pl), "temp_capable": l.temp_capable, "channel": l.channel}


def freshness_from_sync(now: datetime, rows: list[SyncStatus], factor: float = 1.0) -> tuple[float, str, list[dict]]:
    if not rows:
        return 1.0, "no sync tracking configured", []
    scores, notes, detail = [], [], []
    for s in rows:
        exp_h = (s.expected_every_hours or 24) * factor
        age_h = ((now - s.last_sync).total_seconds() / 3600.0) if s.last_sync else None
        if age_h is None:
            score, status = 0.0, "NEVER"
        elif age_h <= exp_h:
            score, status = 1.0, "FRESH"
        else:
            score = max(0.0, 1.0 - (age_h - exp_h) / (2.0 * exp_h))
            status = "STALE"
        scores.append(score)
        detail.append({"source": s.source, "last_sync": s.last_sync, "age_hours": age_h, "expected_hours": exp_h,
                       "status": status, "score": score, "records": s.records, "note": s.note})
        if status != "FRESH":
            notes.append(f"{s.source} {status.lower()}" + (f" ({age_h:.0f} h old)" if age_h is not None else ""))
    return sum(scores) / len(scores), ("all sources fresh" if not notes else "; ".join(notes)), detail


def build_snapshot() -> Snapshot:
    cfg = config_from_settings()
    today = cfg.today
    now = S.now()
    sync_rows = SyncStatus.query.all()
    fr, fnote, fdetail = freshness_from_sync(now, sync_rows, S.get("thresholds.stale_data_factor") or 1.0)
    cfg.freshness, cfg.freshness_note = fr, fnote
    snap = Snapshot(cfg=cfg, today=today, freshness={"score": fr, "note": fnote}, sync=fdetail)

    items = {i.id: i for i in Item.query.all()}
    locs = {l.id: l for l in Location.query.all()}
    sups = {s.id: s for s in Supplier.query.all()}
    custs = {c.id: c for c in Customer.query.all()}
    snap.items = {k: _item_dict(v, today) for k, v in items.items()}
    snap.locs = {k: _loc_dict(v) for k, v in locs.items()}
    snap.suppliers = {k: {"id": v.id, "code": v.code, "name": v.name, "country": v.country, "region": v.region, "tier": v.tier,
                          "lat": v.lat, "lon": v.lon, "geo_risk": v.geo_risk, "quality_rejection_rate": v.quality_rejection_rate,
                          "payment_terms_days": v.payment_terms_days, "industry": v.industry} for k, v in sups.items()}
    snap.customers = {k: {"id": v.id, "code": v.code, "name": v.name, "priority": v.priority, "segment": v.segment,
                          "region": v.region, "contract_priority": bool(v.contract_priority)} for k, v in custs.items()}

    pairs: dict[tuple, PairInputs] = {}

    def P(item_id, loc_id) -> PairInputs | None:
        if item_id not in items or loc_id not in locs:
            return None
        k = (item_id, loc_id)
        if k not in pairs:
            pairs[k] = PairInputs(item_id=item_id, location_id=loc_id, sku=items[item_id].sku, loc_code=locs[loc_id].code,
                                  item=snap.items[item_id], loc=snap.locs[loc_id])
        return pairs[k]

    # balances & lots -------------------------------------------------------------------------------------------
    lot_rows = {l.id: l for l in Lot.query.all()}
    for b in InventoryBalance.query.filter(InventoryBalance.quantity != 0).all():
        p = P(b.item_id, b.location_id)
        if not p:
            continue
        p.stock[b.state] = p.stock.get(b.state, 0.0) + b.quantity
        lot = lot_rows.get(b.lot_id)
        received = (lot.received_date if lot and lot.received_date else (b.last_receipt_at.date() if b.last_receipt_at else None))
        p.lots.append({"lot_id": b.lot_id, "lot_no": lot.lot_no if lot else None, "qty": b.quantity, "state": b.state,
                       "expiry": lot.expiry_date if lot else None, "received": received,
                       "quality": lot.quality_status if lot else "RELEASED", "supplier_id": lot.supplier_id if lot else None})
        if b.last_movement_at:
            dsm = (now - b.last_movement_at).days
            p.days_since_movement = dsm if p.days_since_movement is None else min(p.days_since_movement, dsm)
        if b.last_receipt_at:
            dsr = (now - b.last_receipt_at).days
            p.days_since_receipt = dsr if p.days_since_receipt is None else min(p.days_since_receipt, dsr)

    # allocations ------------------------------------------------------------------------------------------------
    for a in Allocation.query.filter(Allocation.status == "ACTIVE").all():
        p = P(a.item_id, a.location_id)
        if not p:
            continue
        if a.alloc_type == "COMMITTED":
            p.committed += a.qty
        elif a.alloc_type == "RESERVED":
            p.reserved += a.qty
        else:
            p.allocated += a.qty

    # shipments index --------------------------------------------------------------------------------------------
    ship_idx: dict[tuple, Shipment] = {}
    for s in Shipment.query.filter(Shipment.status != "DELIVERED").all():
        for ln in s.lines:
            ship_idx[(s.ref_number, ln.item_id)] = s

    # purchase orders --------------------------------------------------------------------------------------------
    def day(d: date | None):
        return None if d is None else (d - today).days

    for po in PurchaseOrder.query.filter(PurchaseOrder.status.in_(["OPEN", "PARTIAL"])).all():
        sup = sups.get(po.supplier_id)
        for ln in po.lines:
            open_q = ln.qty_ordered - (ln.qty_received or 0.0)
            if open_q <= 1e-9:
                continue
            p = P(ln.item_id, po.dest_location_id)
            if not p:
                continue
            eta = ln.eta_date or po.eta_date or ln.promised_date or po.promised_date
            prom = ln.promised_date or po.promised_date or eta
            ship = ship_idx.get((po.po_number, ln.item_id))
            base = {"kind": "PO", "ref": po.po_number, "supplier_id": po.supplier_id, "lane": (ship.lane if ship else po.lane),
                    "mode": po.mode, "origin": sup.city if sup else None, "po_id": po.id, "line_id": ln.id,
                    "eta_day": day(eta) if eta else 0, "promised_day": day(prom) if prom else None,
                    "expedited": po.expedited, "delay_reason": (ship.delay_reason if ship and ship.delay_reason else po.delay_reason),
                    "delay_days": (ship.delay_days if ship else 0) or 0, "port": ship.port if ship else None,
                    "shipment_no": ship.shipment_no if ship else None}
            it_q = min(ln.qty_in_transit or 0.0, open_q)
            if it_q > 1e-9:
                p.inbound.append({**base, "qty": it_q, "in_transit": True})
            oo = open_q - it_q
            if oo > 1e-9:
                p.inbound.append({**base, "qty": oo, "in_transit": False, "shipment_no": None})

    # transfer orders --------------------------------------------------------------------------------------------
    for t in TransferOrder.query.filter(TransferOrder.status.in_(["PLANNED", "OPEN", "IN_TRANSIT"])).all():
        open_q = t.qty - (t.qty_received or 0.0)
        if open_q <= 1e-9:
            continue
        ship = ship_idx.get((t.to_number, t.item_id))
        dest = P(t.item_id, t.to_location_id)
        if dest:
            eta = t.eta_date or t.promised_date
            dest.inbound.append({"kind": "TO", "ref": t.to_number, "qty": open_q, "in_transit": t.status == "IN_TRANSIT",
                                 "eta_day": day(eta) if eta else 0, "promised_day": day(t.promised_date or eta),
                                 "supplier_id": None, "lane": ship.lane if ship else None, "mode": t.mode,
                                 "origin": locs[t.from_location_id].code if t.from_location_id in locs else None,
                                 "delay_days": ship.delay_days if ship else 0, "delay_reason": ship.delay_reason if ship else None,
                                 "shipment_no": ship.shipment_no if ship else None, "port": ship.port if ship else None,
                                 "from_location_id": t.from_location_id})
        if t.status in ("PLANNED", "OPEN"):
            src = P(t.item_id, t.from_location_id)
            if src:
                src.reserved += open_q
                src.transfers_out.append({"qty": open_q, "day": max(0, day(t.ship_date) or 0), "ref": t.to_number})

    # production orders & BOM -----------------------------------------------------------------------------------------
    boms = defaultdict(list)
    for b in BomLine.query.all():
        boms[b.parent_item_id].append(b)
    for po in ProductionOrder.query.filter(ProductionOrder.status.in_(["OPEN", "RELEASED"])).all():
        open_q = po.qty - (po.qty_completed or 0.0)
        if open_q <= 1e-9:
            continue
        p = P(po.item_id, po.location_id)
        if p:
            p.inbound.append({"kind": "PROD", "ref": po.order_number, "qty": open_q, "in_transit": False,
                              "eta_day": day(po.due_date) or 0, "promised_day": day(po.due_date), "supplier_id": None,
                              "lane": None, "mode": None, "origin": None, "delay_days": 0, "prod_order": po.order_number})
        start = po.start_date or po.due_date
        for b in boms.get(po.item_id, []):
            if b.is_alternate:
                continue
            if start and ((b.valid_from and start < b.valid_from) or (b.valid_to and start > b.valid_to)):
                continue
            cp = P(b.component_item_id, po.location_id)
            if cp:
                cp.requirements.append({"qty": open_q * b.qty_per * (1 + (b.scrap_pct or 0.0)), "due_day": max(0, day(start) or 0),
                                        "ref": po.order_number, "parent_item_id": po.item_id, "priority": po.priority})

    # sales orders -----------------------------------------------------------------------------------------------
    for so in SalesOrder.query.filter(SalesOrder.status.in_(["OPEN", "PARTIAL"])).all():
        cust = custs.get(so.customer_id)
        for ln in so.lines:
            open_q = ln.qty_ordered - (ln.qty_shipped or 0.0)
            if open_q <= 1e-9:
                continue
            p = P(ln.item_id, so.ship_from_location_id)
            if not p:
                continue
            due = ln.requested_date or so.requested_date
            price = ln.unit_price or p.item.get("selling_price") or 0.0
            entry = {"qty": open_q, "due_day": day(due) if due else 0, "priority": so.priority, "customer_id": so.customer_id,
                     "ref": so.so_number, "line_id": ln.id, "value": open_q * price, "customer_priority": cust.priority if cust else 3,
                     "channel": so.channel, "order_date": so.order_date, "due": due}
            if due and due < today:
                p.backorder += open_q
                p.extra.setdefault("backorders", []).append(entry)
            else:
                p.orders.append(entry)

    # demand history (weekly, complete weeks only) -----------------------------------------------------------------
    cur_w = week_start(today)
    hist: dict[tuple, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    last_dem: dict[tuple, date] = {}
    for d in Demand.query.filter(Demand.period_start < cur_w + timedelta(days=7)).all():
        w = week_start(d.period_start)
        if w >= cur_w:
            continue
        hist[(d.item_id, d.location_id)][w] += d.qty
        if d.qty > 0:
            k = (d.item_id, d.location_id)
            end = d.period_start + timedelta(days=(6 if d.granularity == "W" else 0))
            if k not in last_dem or end > last_dem[k]:
                last_dem[k] = end
    for k, wk in hist.items():
        p = P(*k)
        if not p:
            continue
        ws = sorted(wk)
        first = max(ws[0], cur_w - timedelta(weeks=52))
        weeks = []
        w = first
        while w < cur_w:
            weeks.append(w)
            w += timedelta(days=7)
        p.hist_weekly = [wk.get(w, 0.0) for w in weeks]
        p.extra["hist_weeks"] = weeks
        if k in last_dem:
            p.days_since_last_demand = max(0, (today - last_dem[k]).days)
        elif ws:
            p.days_since_last_demand = (today - ws[-1]).days

    # forecasts -------------------------------------------------------------------------------------------------------
    prec = S.get("engine.forecast_precedence")
    rank = {t: i for i, t in enumerate(prec)}
    fc: dict[tuple, dict[date, tuple[int, float, str]]] = defaultdict(dict)
    for f in Forecast.query.filter(Forecast.granularity == "W").all():
        w = week_start(f.period_start)
        k = (f.item_id, f.location_id)
        cand = (rank.get(f.forecast_type, 99), f.qty, f.forecast_type)
        if w not in fc[k] or cand[0] < fc[k][w][0]:
            fc[k][w] = cand
    for k, wk in fc.items():
        p = P(*k)
        if not p:
            continue
        fut = [w for w in wk if w >= cur_w]
        if fut:
            weeks = sorted(fut)
            arr, w = [], cur_w
            last = wk[weeks[0]][1]
            for _ in range(int(cfg.horizon_weeks) + 4):
                if w in wk:
                    last = wk[w][1]
                arr.append(last)
                w += timedelta(days=7)
            p.forecast_weekly = arr
            p.forecast_type = wk[weeks[0]][2]
        hw = p.extra.get("hist_weeks") or []
        actual = dict(zip(hw, p.hist_weekly))
        p.fc_pairs = [(wk[w][1], actual[w]) for w in sorted(wk) if w < cur_w and w in actual][-26:]

    # sourcing, moq/multiple, lead-time observations -----------------------------------------------------------------
    isup: dict[int, list[ItemSupplier]] = defaultdict(list)
    for r in ItemSupplier.query.all():
        isup[r.item_id].append(r)
    ilsrc = {(r.item_id, r.location_id): r for r in ItemLocationSource.query.all()}
    obs_idx: dict[tuple, list] = defaultdict(list)
    for o in LeadTimeObservation.query.all():
        rec = (o.lead_time_days, o.promised_days)
        if o.supplier_id:
            obs_idx[("S", o.supplier_id, o.item_id, o.dest_location_id)].append(rec)
            obs_idx[("SI", o.supplier_id, o.item_id)].append(rec)
            obs_idx[("SU", o.supplier_id)].append(rec)
        if o.origin and o.dest_location_id:
            obs_idx[("L", o.origin, o.dest_location_id)].append(rec)
    min_obs = cfg.min_lt_obs
    ss_pol = PolicyIndex(SafetyStockPolicy.query.all())
    rp_pol = PolicyIndex(ReplenishmentPolicy.query.all())
    for k, p in pairs.items():
        lc = locs[p.location_id]
        it = items[p.item_id]
        src = ilsrc.get(k)
        primary = next((r for r in isup.get(p.item_id, []) if r.is_primary), (isup.get(p.item_id) or [None])[0])
        lt_list = None
        if src and src.source_location_id:
            p.source_loc_id = src.source_location_id
            p.static_lt = src.lead_time_days
            sl = locs.get(src.source_location_id)
            lt_list = obs_idx.get(("L", sl.code if sl else "", p.location_id))
            if sl:
                p.distance_km = route_km(sl.lat, sl.lon, lc.lat, lc.lon, "ROAD")
            p.mode = "ROAD"
            p.moq = it.moq
            p.multiple = it.order_multiple
        else:
            sup_id = (src.supplier_id if src and src.supplier_id else (primary.supplier_id if primary else None))
            p.supplier_id = sup_id
            rel = next((r for r in isup.get(p.item_id, []) if r.supplier_id == sup_id), primary)
            if rel:
                p.static_lt = (src.lead_time_days if src and src.lead_time_days else rel.lead_time_days)
                p.moq, p.multiple = rel.moq or it.moq, rel.order_multiple or it.order_multiple
                p.supplier_capacity_week = rel.capacity_units_week
                p.mode = rel.mode or "ROAD"
            sp = sups.get(sup_id)
            if sp:
                p.distance_km = route_km(sp.lat, sp.lon, lc.lat, lc.lon, p.mode)
            for key in (("S", sup_id, p.item_id, p.location_id), ("SI", sup_id, p.item_id), ("SU", sup_id)):
                cand = obs_idx.get(key)
                if cand and (len(cand) >= min_obs or key[0] == "SU"):
                    lt_list = cand
                    break
                if cand and lt_list is None:
                    lt_list = cand
        if lt_list:
            p.lt_obs = [a for a, _ in lt_list]
            p.lt_promised = [b for _, b in lt_list]
        item_dict, loc_dict = p.item, p.loc
        pol, _ = ss_pol.resolve(item_dict, loc_dict)
        p.policy = pol
        rpol, _ = rp_pol.resolve(item_dict, loc_dict)
        p.repl = rpol

    snap.inputs = pairs
    for k, p in pairs.items():
        snap.results[k] = compute_pair(p, cfg)
    _segment(snap)
    return snap


def _segment(snap: Snapshot) -> None:
    """ABC / XYZ / FSN / HML at item level (aggregated over all locations)."""
    cfg = snap.cfg
    annual: dict[int, float] = defaultdict(float)
    series: dict[int, list[float]] = defaultdict(list)
    turns_inv: dict[int, float] = defaultdict(float)
    dsm: dict[int, float] = {}
    for (iid, lid), inp in snap.inputs.items():
        h = inp.hist_weekly[-52:]
        annual[iid] += sum(h) * (52.0 / max(len(h), 1)) if h else 0.0
        cur = series[iid]
        if len(cur) < len(h):
            cur[:0] = [0.0] * (len(h) - len(cur))
        pad = len(cur) - len(h)
        for j, v in enumerate(h):
            cur[pad + j] += v
        turns_inv[iid] += snap.results[(iid, lid)].pos["on_hand"]
        if inp.days_since_last_demand is not None:
            dsm[iid] = min(dsm.get(iid, 1e9), inp.days_since_last_demand)
    rows = [{"key": iid, "annual_demand": annual[iid], "unit_cost": snap.items[iid]["unit_cost"] or 0.0} for iid in snap.items]
    abc = {r["key"]: r for r in seg.abc_classify(rows, S.get("abc.thresholds"))}
    xyz = {r["key"]: r for r in seg.xyz_classify([{"key": iid, "series": series.get(iid, [])} for iid in snap.items],
                                                  S.get("xyz.thresholds"))}
    hml = seg.hml_classify({iid: snap.items[iid]["unit_cost"] or 0.0 for iid in snap.items}, S.get("hml.thresholds"))
    fsn_th = S.get("fsn.thresholds")
    for iid, it in snap.items.items():
        oh = turns_inv.get(iid, 0.0)
        turns = (annual[iid] / oh) if oh > 0 else (99.0 if annual[iid] > 0 else 0.0)
        snap.item_seg[iid] = {
            "abc": abc[iid]["abc"], "xyz": xyz[iid]["xyz"], "annual_demand": annual[iid],
            "annual_value": abc[iid]["annual_value"], "cum_pct": abc[iid]["cum_pct"], "cv": xyz[iid]["cv"],
            "fsn": seg.fsn_classify(turns, dsm.get(iid), fsn_th), "turns": turns, "hml": hml[iid],
            "ved": it.get("ved"), "sde": it.get("sde"), "criticality": it.get("criticality"),
        }
