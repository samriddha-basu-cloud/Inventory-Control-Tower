"""Supplier performance / risk score and the unified inventory risk register.

Supplier risk score (0–100, higher = riskier) = Σ weight_i × risk_i × 100, all components 0–1 and documented:
    otif            1 − OTIF (lines on time AND in full, last 12 months)
    lt_reliability  1 − share of receipts within promised lead time (+1 d)
    quality         min(1, quality rejection rate ÷ 5 %)
    concentration   min(1, number of critical/high SKUs that are single-sourced with this supplier ÷ 5)
    geo             configured country/lane risk (Supplier.geo_risk)
    expedite        min(1, share of expedited receipts ÷ 30 %)
Weights are configuration (weights.supplier_risk).
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import timedelta

from ..extensions import db
from ..models import ItemSupplier, LeadTimeObservation, PurchaseOrder, PurchaseOrderLine, Risk, Shipment
from ..rules.thresholds import worse
from ..utils.stats import clamp, mean, std
from . import settings_service as S
from .snapshot import Snapshot

RISK_TYPES = ["STOCKOUT", "EXCESS", "OBSOLESCENCE", "EXPIRY", "SUPPLIER_DELAY", "TRANSPORT_DELAY", "QUALITY_HOLD", "DEMAND_SPIKE",
              "DEMAND_COLLAPSE", "LEAD_TIME_INCREASE", "CAPACITY_CONSTRAINT", "SINGLE_SOURCING", "GEOPOLITICAL", "WEATHER", "PORT"]
OWNER = {"STOCKOUT": "Inventory Planner", "EXCESS": "Supply Chain Manager", "OBSOLESCENCE": "Supply Chain Manager", "EXPIRY": "Warehouse Manager",
         "SUPPLIER_DELAY": "Procurement", "TRANSPORT_DELAY": "Operations", "QUALITY_HOLD": "Warehouse Manager", "DEMAND_SPIKE": "Demand Planner",
         "DEMAND_COLLAPSE": "Demand Planner", "LEAD_TIME_INCREASE": "Procurement", "CAPACITY_CONSTRAINT": "Warehouse Manager",
         "SINGLE_SOURCING": "Procurement", "GEOPOLITICAL": "Supply Chain Manager", "WEATHER": "Operations", "PORT": "Operations"}
ACTION = {"STOCKOUT": "Replenish / expedite / transfer", "EXCESS": "Rebalance or stop replenishment", "OBSOLESCENCE": "Review disposition (never auto-scrap)",
          "EXPIRY": "FEFO push, transfer or markdown review", "SUPPLIER_DELAY": "Escalate to supplier; consider alternate source",
          "TRANSPORT_DELAY": "Re-route or expedite", "QUALITY_HOLD": "Complete inspection; release or reject", "DEMAND_SPIKE": "Review forecast & safety stock",
          "DEMAND_COLLAPSE": "Freeze replenishment; review exposure", "LEAD_TIME_INCREASE": "Raise safety stock / update master lead time",
          "CAPACITY_CONSTRAINT": "Transfer out or add space", "SINGLE_SOURCING": "Qualify a second source", "GEOPOLITICAL": "Buffer stock / dual-source",
          "WEATHER": "Pre-position stock", "PORT": "Re-route via alternate port"}


def sev_from_exposure(x: float) -> str:
    th = S.get("thresholds.risk_exposure") or {"medium": 50000, "high": 500000, "critical": 2000000}
    return "CRITICAL" if x >= th["critical"] else "HIGH" if x >= th["high"] else "MEDIUM" if x >= th["medium"] else "LOW"


def band(score: float) -> str:
    return "CRITICAL" if score >= 65 else "HIGH" if score >= 45 else "MEDIUM" if score >= 25 else "LOW"


# ---------------------------------------------------------------------------------------------------------- suppliers
def supplier_scorecards(snap: Snapshot) -> dict[int, dict]:
    w = S.get("weights.supplier_risk")
    today = snap.today
    obs = defaultdict(list)
    for o in LeadTimeObservation.query.filter(LeadTimeObservation.supplier_id.isnot(None)).all():
        if o.received_date and o.received_date >= today - timedelta(days=365):
            obs[o.supplier_id].append(o)
    single = defaultdict(int)
    for item_id, it in snap.items.items():
        if it["criticality"] in ("Critical", "High"):
            n_sup = ItemSupplier.query.filter_by(item_id=item_id).count()
            if n_sup <= 1:
                for r in ItemSupplier.query.filter_by(item_id=item_id):
                    single[r.supplier_id] += 1
    open_po = defaultdict(lambda: {"count": 0, "value": 0.0, "delayed": 0})
    for po in PurchaseOrder.query.filter(PurchaseOrder.status.in_(["OPEN", "PARTIAL"])).all():
        for ln in po.lines:
            oq = ln.qty_ordered - (ln.qty_received or 0)
            if oq <= 0:
                continue
            d = open_po[po.supplier_id]
            d["count"] += 1
            d["value"] += oq * (ln.unit_price or 0)
            eta, prom = (ln.eta_date or po.eta_date), (ln.promised_date or po.promised_date)
            if (eta and prom and eta > prom) or (prom and prom < today):
                d["delayed"] += 1
    out = {}
    for sid, s in snap.suppliers.items():
        rows = obs.get(sid, [])
        n = len(rows)
        on_time = sum(1 for o in rows if o.lead_time_days <= (o.promised_days or o.lead_time_days) + 1)
        in_full = sum(1 for o in rows if (o.qty_received or 0) >= 0.95 * (o.qty_ordered or 0))
        otif_n = sum(1 for o in rows if o.lead_time_days <= (o.promised_days or o.lead_time_days) + 1 and (o.qty_received or 0) >= 0.95 * (o.qty_ordered or 0))
        lts = [o.lead_time_days for o in rows]
        acc = mean([(o.lead_time_days - (o.promised_days or o.lead_time_days)) / (o.promised_days or 1) for o in rows]) if rows else None
        rej = (sum(o.rejected_qty or 0 for o in rows) / max(sum(o.qty_received or 0 for o in rows), 1)) if rows else s.get("quality_rejection_rate", 0.0)
        exp_freq = (sum(1 for o in rows if o.expedited) / n) if n else 0.0
        fill = (sum(o.qty_received or 0 for o in rows) / max(sum(o.qty_ordered or 0 for o in rows), 1)) if rows else None
        comp = {
            "otif": 1 - (otif_n / n) if n else 0.3, "lt_reliability": 1 - (on_time / n) if n else 0.3,
            "quality": clamp(rej / 0.05), "concentration": clamp(single.get(sid, 0) / 5.0), "geo": clamp(s.get("geo_risk") or 0.0),
            "expedite": clamp(exp_freq / 0.30),
        }
        tw = sum(w.values()) or 1.0
        score = 100.0 * sum(comp[k] * w.get(k, 0.0) for k in comp) / tw
        spend = open_po[sid]["value"]
        crit = [i["sku"] for iid, i in snap.items.items() if i["criticality"] in ("Critical", "High") and
                ItemSupplier.query.filter_by(item_id=iid, supplier_id=sid).first()]
        out[sid] = {"supplier_id": sid, "code": s["code"], "name": s["name"], "country": s["country"], "tier": s["tier"], "n_obs": n,
                    "otif": otif_n / n if n else None, "on_time": on_time / n if n else None, "in_full": in_full / n if n else None,
                    "fill_rate": fill, "lt_accuracy": acc, "lt_mean": mean(lts) if lts else None, "lt_std": std(lts) if lts else None,
                    "lt_cv": (std(lts) / mean(lts)) if lts and mean(lts) else None, "lt_p90": sorted(lts)[int(0.9 * (len(lts) - 1))] if lts else None,
                    "quality_rejection": rej, "expedite_freq": exp_freq, "open_pos": open_po[sid]["count"], "open_value": spend,
                    "delayed_lines": open_po[sid]["delayed"], "critical_skus": crit, "risk_score": score, "risk_band": band(score), "components": comp,
                    "weights": w, "geo_risk": s.get("geo_risk")}
    return out


# ---------------------------------------------------------------------------------------------------------- register
def _rid(*parts) -> str:
    return "R-" + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:8].upper()


def build_risk_register(snap: Snapshot, scorecards: dict | None = None) -> int:
    """Rebuild the risk register from current state. User-set statuses (ACKNOWLEDGED/MITIGATING/ACCEPTED) survive rebuilds."""
    scorecards = scorecards or supplier_scorecards(snap)
    existing = {r.risk_id: r for r in Risk.query.all()}
    keep_status = {rid: r.status for rid, r in existing.items() if r.status not in ("OPEN", "CLOSED")}
    Risk.query.delete()
    db.session.flush()
    rows = []

    def add(rtype, item_id, loc_id, sup_id, cause, prob, impact, key):
        expo = prob * impact
        if expo <= 0:
            return
        rows.append(Risk(risk_id=_rid(rtype, *key), risk_type=rtype, item_id=item_id, location_id=loc_id, supplier_id=sup_id, cause=cause,
                         probability=round(prob, 4), impact_value=round(impact, 2), exposure=round(expo, 2), severity=sev_from_exposure(expo),
                         recommended_action=ACTION[rtype], owner=OWNER[rtype]))

    for k, r in snap.results.items():
        inp = snap.inputs[k]
        iid, lid = k
        if r.risk_level in ("MEDIUM", "HIGH", "CRITICAL") and r.d_mean > 0:
            impact = max(r.lost_sales_value, r.expected_shortage_qty * r.price * snap.cfg.lost_sale_fraction) + r.production_risk
            add("STOCKOUT", iid, lid, inp.supplier_id, f"P(stock-out over {r.lt_plan:.0f} d lead time) = {r.stockout_prob:.0%}; projected min {r.min_projected:,.0f}",
                r.stockout_prob, max(impact, r.d_mean * r.lt_plan * r.price * 0.2), (iid, lid))
        if r.excess_value > 0:
            add("EXCESS", iid, lid, None, f"Stock {r.pos['usable_on_hand']:,.0f} exceeds max/DOS threshold by {r.excess_qty:,.0f}",
                0.5 if r.inv_class == "EXCESS" else 0.8, r.excess_value * snap.cfg.holding_rate + r.obsolescence_exposure, (iid, lid))
        if r.inv_class in ("NON_MOVING", "OBSOLETE"):
            add("OBSOLESCENCE", iid, lid, None, f"{r.inv_class.replace('_', '-').title()}: no demand recently", 0.9 if r.inv_class == "OBSOLETE" else 0.5,
                r.on_hand_value, (iid, lid, "obs"))
        if r.at_risk_qty > 0 or r.expired_qty > 0:
            add("EXPIRY", iid, lid, None, f"{r.at_risk_qty:,.0f} units will expire before consumption; {r.expired_qty:,.0f} already expired",
                0.85, (r.at_risk_qty + r.expired_qty) * r.unit_cost, (iid, lid))
        blocked = (r.quarantined_value + r.blocked_value)
        if blocked > 0:
            add("QUALITY_HOLD", iid, lid, None, "Stock held in quarantine/blocked", 0.4, blocked, (iid, lid))
        if r.lt_stats.get("eligible") and r.lt_static and r.lt_stats.get("mean") and r.lt_stats["mean"] / r.lt_static > 1.25:
            add("LEAD_TIME_INCREASE", iid, lid, inp.supplier_id, f"Observed mean {r.lt_stats['mean']:.0f} d vs master {r.lt_static:.0f} d",
                clamp(r.lt_stats["mean"] / r.lt_static - 1.0), r.d_mean * (r.lt_stats["mean"] - r.lt_static) * r.price, (iid, lid))
        h = inp.hist_weekly
        if len(h) >= 20:
            recent, prior = mean(h[-4:]), mean(h[-26:-4]) or 0.0
            if prior > 0 and recent / prior > 1.5:
                add("DEMAND_SPIKE", iid, lid, None, f"Last 4 wk demand {recent / prior:.1f}× the prior mean", 0.6, (recent - prior) * 4 * r.price, (iid, lid))
            if prior > 0 and recent / prior < 0.5:
                add("DEMAND_COLLAPSE", iid, lid, None, f"Last 4 wk demand {recent / prior:.1f}× the prior mean", 0.6, r.on_hand_value * 0.3, (iid, lid))
    # supplier / transport / port
    delayed_ports = defaultdict(lambda: {"n": 0, "value": 0.0, "days": 0.0})
    for s in Shipment.query.filter(Shipment.status.in_(["DELAYED", "IN_TRANSIT"])).all():
        if (s.delay_days or 0) > 0:
            val = sum(l.qty * (snap.items.get(l.item_id, {}).get("unit_cost") or 0) for l in s.lines)
            add("TRANSPORT_DELAY", None, s.dest_location_id, s.supplier_id, f"{s.shipment_no} on {s.lane}: +{s.delay_days:.0f} d ({s.delay_reason})",
                min(1.0, 0.5 + s.delay_days / 20), val * 2.0, (s.shipment_no,))
            if s.port:
                p = delayed_ports[s.port]
                p["n"] += 1
                p["value"] += val
                p["days"] = max(p["days"], s.delay_days)
    for port, p in delayed_ports.items():
        add("PORT", None, None, None, f"{p['n']} shipments delayed up to {p['days']:.0f} d at {port}", 0.8, p["value"] * 2.5, (port,))
    for sid, sc in scorecards.items():
        if sc["delayed_lines"] > 0:
            add("SUPPLIER_DELAY", None, None, sid, f"{sc['delayed_lines']} open PO line(s) late/slipping; OTIF {sc['otif']:.0%}" if sc["otif"] is not None else "open lines late",
                clamp(1 - (sc["otif"] or 0.7)) + 0.15, sc["open_value"] * 0.25, (sid,))
        if sc["components"]["concentration"] > 0.0:
            add("SINGLE_SOURCING", None, None, sid, f"{len(sc['critical_skus'])} critical/high SKU(s) rely on this supplier alone", 0.08 + 0.4 * sc["components"]["concentration"],
                sc["open_value"] * 2 + 500000, (sid, "ss"))
        if (sc["geo_risk"] or 0) >= 0.22:
            add("GEOPOLITICAL", None, None, sid, f"Configured country/lane risk {sc['geo_risk']:.0%} ({sc['country']})", sc["geo_risk"], sc["open_value"] * 3 + 300000, (sid, "geo"))
    for lid, l in snap.locs.items():
        cap = l.get("capacity_units") or 0
        oh = sum(snap.results[k].pos["on_hand"] for k in snap.keys_for_loc(lid))
        if cap and oh / cap > 0.9:
            add("CAPACITY_CONSTRAINT", None, lid, None, f"Utilisation {oh / cap:.0%} of {cap:,.0f} units", clamp((oh / cap - 0.85) * 4), oh * 20, (lid,))
    for r_ in rows:
        if r_.risk_id in keep_status:
            r_.status = keep_status[r_.risk_id]
    db.session.add_all(rows)
    db.session.flush()
    return len(rows)


def heatmap(snap: Snapshot, by: str = "location", f=None) -> dict:
    """Risk heatmap: rows = SKU | location | supplier | family | region; columns = stockout, excess, expiry, obsolescence, supply, lead time, quality.
    Cell value 0–1 (share of the row's value/pairs at risk); the UI adds icons + numbers so colour is never the only cue."""
    cols = ["Stockout", "Excess", "Expiry", "Obsolescence", "Supply", "Lead time", "Quality"]
    groups: dict[str, list] = defaultdict(list)
    for inp, r in snap.rows(f):
        if by == "sku":
            g = inp.sku
        elif by == "location":
            g = inp.loc_code
        elif by == "supplier":
            g = snap.suppliers.get(inp.supplier_id, {}).get("code", "(internal)")
        elif by == "family":
            g = inp.item.get("family_code") or "?"
        else:
            g = inp.loc.get("region") or "?"
        groups[g].append((inp, r))
    rows = []
    for g, prs in sorted(groups.items()):
        val = sum(r.on_hand_value for _, r in prs) or 1.0
        dem = [(i, r) for i, r in prs if r.d_mean > 0]
        n_dem = len(dem) or 1
        delayed = sum(1 for i, _ in prs for ib in i.inbound if (ib.get("delay_days") or 0) > 0 or (ib.get("promised_day") is not None and ib["promised_day"] < 0))
        inbound_n = sum(len(i.inbound) for i, _ in prs) or 1
        lt_bad = sum(1 for _, r in prs if r.lt_stats.get("eligible") and r.lt_static and r.lt_stats["mean"] / r.lt_static > 1.15)
        vals = [
            mean([r.stockout_prob for _, r in dem]) if dem else 0.0,
            sum(r.excess_value for _, r in prs) / val,
            sum(r.at_risk_value + r.expired_qty * r.unit_cost for _, r in prs) / val,
            sum(r.obsolescence_exposure for _, r in prs) / val,
            delayed / inbound_n,
            lt_bad / len(prs),
            sum(r.quarantined_value + r.blocked_value for _, r in prs) / val,
        ]
        rows.append({"key": g, "values": vals, "n": len(prs), "value": val})
    return {"columns": cols, "rows": rows, "by": by}
