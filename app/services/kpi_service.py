"""KPI engine. Formulas, thresholds, windows and alert thresholds are configuration (KpiDefinition rows).

The engine computes a dictionary of *measures* from operational data; each KPI is a safe expression over
those measures (rules/rule_engine.safe_eval - no eval/exec). Status is Normal / Watch / Attention / Critical.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import timedelta

from ..models import BomLine, ExternalBalance, KpiDefinition, KpiSnapshot, LeadTimeObservation, SalesOrder, SalesOrderLine
from ..rules.rule_engine import FormulaError, safe_eval
from ..rules.thresholds import kpi_status
from ..utils.stats import mean, std
from . import settings_service as S
from .snapshot import PairFilter, Snapshot

MEASURE_DOCS = {
    "inventory_value": "Σ on-hand value (all counted states) at unit cost",
    "avg_inventory_value": "Average of weekly inventory-value snapshots in the KPI window (falls back to current)",
    "cogs_annual": "Annualised cost of goods for customer-facing item-locations (excludes BOM components to avoid double counting)",
    "sales_annual": "Annualised revenue at selling price, same scope as cogs_annual",
    "on_hand_value": "Current on-hand value", "in_transit_value": "Value of owned in-transit stock",
    "excess_value": "Value of stock beyond max level and days-of-supply threshold", "slow_value": "On-hand value of SLOW items",
    "obsolete_value": "On-hand value of OBSOLETE items", "holding_rate": "Configured annual holding-cost rate",
    "lines_total": "Shipped/closed order lines in window", "lines_in_full": "…of which shipped in full",
    "lines_otif": "…of which shipped on time and in full", "units_ordered": "Units ordered on those lines",
    "units_shipped": "Units shipped", "units_backordered": "Past-due open order units",
    "stockout_pairs": "Item-locations with demand and no usable stock", "demand_pairs": "Item-locations with demand",
    "accuracy_matched": "Physical counts within tolerance of ICT", "accuracy_total": "Physical counts compared",
    "forecast_minus_actual": "Σ(forecast − actual) over paired weeks", "actual_sum": "Σ actual demand over paired weeks",
    "supplier_lines_total": "Supplier receipts in window", "supplier_lines_otif": "…received on time and in full",
    "lt_std": "Mean σ of observed lead time per supplier-item", "lt_mean": "Mean observed lead time per supplier-item",
    "lt_on_time": "Receipts within promised lead time (+1 d)", "lt_total": "Receipts observed",
    "order_cycle_days_sum": "Σ days from order to delivery", "order_cycle_count": "Delivered lines counted",
    "risk_weighted": "Σ stock-out probability × daily demand value", "risk_weight": "Σ daily demand value",
    "lost_sales_value": "Σ expected lost sales value over lead time",
}


def terminal_pairs(snap: Snapshot) -> set[tuple]:
    referenced = {(inp.item_id, inp.source_loc_id) for inp in snap.inputs.values() if inp.source_loc_id}
    return {k for k in snap.inputs if k not in referenced}


def measures(snap: Snapshot, f: PairFilter | None = None, window_days: int = 90) -> dict:
    today = snap.today
    keys = snap.keys(f)
    m = defaultdict(float, {k: 0.0 for k in MEASURE_DOCS})
    components = {r.component_item_id for r in BomLine.query.all()}
    term = terminal_pairs(snap)
    tot = snap.totals(f)
    m["inventory_value"] = m["on_hand_value"] = tot.get("on_hand_value", 0.0)
    m["in_transit_value"] = tot.get("in_transit_value", 0.0)
    m["excess_value"] = tot.get("excess_value", 0.0)
    m["obsolete_value"] = tot.get("obsolete_value", 0.0)
    m["lost_sales_value"] = tot.get("lost_sales_value", 0.0)
    m["holding_rate"] = snap.cfg.holding_rate
    pair_hist = {}
    for k in keys:
        inp, r = snap.inputs[k], snap.results[k]
        if r.inv_class in ("SLOW", "NON_MOVING"):
            m["slow_value"] += r.on_hand_value
        if k in term and inp.item_id not in components:
            h = inp.hist_weekly[-52:]
            annual = sum(h) * (52.0 / max(len(h), 1)) if h else 0.0
            m["cogs_annual"] += annual * r.unit_cost
            m["sales_annual"] += annual * r.price
        if r.d_mean > 0:
            m["demand_pairs"] += 1
            if r.pos["usable_on_hand"] <= 0:
                m["stockout_pairs"] += 1
            m["risk_weighted"] += r.stockout_prob * r.d_mean * max(r.price, r.unit_cost, 1e-6)
            m["risk_weight"] += r.d_mean * max(r.price, r.unit_cost, 1e-6)
        m["units_backordered"] += inp.backorder
        for fc, a in inp.fc_pairs[-13:]:
            m["forecast_minus_actual"] += fc - a
            m["actual_sum"] += a
    # average inventory over window (weekly snapshots) --------------------------------------------------------------
    pts = KpiSnapshot.query.filter(KpiSnapshot.metric == "inventory_value", KpiSnapshot.as_of >= today - timedelta(days=window_days)).all()
    scale = (m["inventory_value"] / tot["on_hand_value"]) if tot.get("on_hand_value") else 1.0
    m["avg_inventory_value"] = mean([p.value for p in pts]) if len(pts) >= 4 and (f is None or f.is_empty()) else m["inventory_value"]
    # service from order lines ---------------------------------------------------------------------------------------
    item_ids = {snap.inputs[k].item_id for k in keys}
    loc_ids = {snap.inputs[k].location_id for k in keys}
    q = (SalesOrderLine.query.join(SalesOrder, SalesOrder.id == SalesOrderLine.so_id)
         .filter(SalesOrder.status == "SHIPPED", SalesOrderLine.requested_date >= today - timedelta(days=window_days))
         .with_entities(SalesOrderLine.item_id, SalesOrder.ship_from_location_id, SalesOrderLine.qty_ordered, SalesOrderLine.qty_shipped,
                        SalesOrderLine.requested_date, SalesOrderLine.ship_date, SalesOrderLine.delivered_date, SalesOrder.order_date))
    for iid, lid, qo, qs, req, shp, dlv, od in q.all():
        if iid not in item_ids or lid not in loc_ids or (iid, lid) not in set(keys):
            continue
        m["lines_total"] += 1
        m["units_ordered"] += qo
        m["units_shipped"] += qs
        full = qs >= qo * 0.999
        m["lines_in_full"] += 1 if full else 0
        m["lines_otif"] += 1 if (full and shp and req and shp <= req) else 0
        if dlv and od:
            m["order_cycle_days_sum"] += (dlv - od).days
            m["order_cycle_count"] += 1
    # accuracy (physical counts) ---------------------------------------------------------------------------------------
    sku_by_id = {i["sku"]: iid for iid, i in snap.items.items()}
    loc_by_code = {l["code"]: lid for lid, l in snap.locs.items()}
    for eb in ExternalBalance.query.filter_by(system="PHYSICAL").all():
        k = (sku_by_id.get(eb.sku_raw), loc_by_code.get(eb.location_raw))
        if k in snap.results and k in set(keys):
            ict = snap.results[k].pos["on_hand"]
            m["accuracy_total"] += 1
            if abs(eb.quantity - ict) <= max(2.0, 0.02 * abs(ict)):
                m["accuracy_matched"] += 1
    # supplier receipts / lead-time ------------------------------------------------------------------------------------
    grp = defaultdict(list)
    for o in LeadTimeObservation.query.filter(LeadTimeObservation.supplier_id.isnot(None)).all():
        if o.item_id not in item_ids:
            continue
        if o.received_date and o.received_date >= today - timedelta(days=window_days * 2):
            m["supplier_lines_total"] += 1
            ok_t = o.lead_time_days <= (o.promised_days or o.lead_time_days) + 1
            ok_q = (o.qty_received or 0) >= 0.95 * (o.qty_ordered or 0)
            m["lt_total"] += 1
            m["lt_on_time"] += 1 if ok_t else 0
            m["supplier_lines_otif"] += 1 if (ok_t and ok_q) else 0
        grp[(o.supplier_id, o.item_id)].append(o.lead_time_days)
    stds = [std(v) for v in grp.values() if len(v) >= 5]
    means = [mean(v) for v in grp.values() if len(v) >= 5]
    m["lt_std"], m["lt_mean"] = mean(stds), mean(means)
    return dict(m)


def compute_all(snap: Snapshot, f: PairFilter | None = None, only: set[str] | None = None) -> dict:
    defs = KpiDefinition.query.filter_by(enabled=True).all()
    cache: dict[int, dict] = {}
    out = {}
    for d in defs:
        if only and d.code not in only:
            continue
        win = d.window_days or 90
        if win not in cache:
            cache[win] = measures(snap, f, win)
        val, err = None, None
        try:
            val = safe_eval(d.formula, cache[win])
            if math.isnan(val) or math.isinf(val):
                val = None
        except ZeroDivisionError:
            err = "insufficient data (zero denominator)"
        except FormulaError as e:
            err = str(e)
        judged = abs(val) if (d.code == "forecast_bias" and val is not None) else val
        status = kpi_status(judged, d.direction, d.watch, d.attention, d.critical) if val is not None else "NORMAL"
        out[d.code] = {"code": d.code, "name": d.name, "category": d.category, "value": val, "unit": d.unit, "status": status if val is not None else "NODATA",
                       "formula": d.formula, "direction": d.direction, "window_days": win, "watch": d.watch, "attention": d.attention,
                       "critical": d.critical, "alert_threshold": d.alert_threshold, "description": d.description, "error": err,
                       "breach": bool(val is not None and d.alert_threshold is not None and
                                      ((d.direction == "higher" and judged < d.alert_threshold) or (d.direction != "higher" and judged > d.alert_threshold)))}
    return out


def validate_formula(formula: str) -> tuple[bool, str]:
    """Dry-run a formula against zero-filled measures to catch syntax/unknown-measure errors before saving."""
    probe = {k: 1.0 for k in MEASURE_DOCS}
    try:
        safe_eval(formula, probe)
        return True, "ok"
    except (FormulaError, ZeroDivisionError) as e:
        return False, str(e) if isinstance(e, FormulaError) else "division by zero on probe values"
