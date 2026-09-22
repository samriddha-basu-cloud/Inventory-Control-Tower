"""Financial impact and sustainability (carbon + circular inventory)."""
from __future__ import annotations

from collections import defaultdict

from flask import Blueprint, render_template, request

from ..models import ReturnRecord, ReusableAsset
from ..services import carbon_service, financial_service, optimization_service as opt, settings_service as S
from ..utils import charts
from . import helpers as H

bp = Blueprint("finance", __name__)


@bp.route("/financial")
def financial():
    if not H.has_data():
        return render_template("finance/financial.html", empty=True)
    snap, f = H.snap(), H.flt()
    fin = financial_service.overview(snap, f)
    wf = charts.waterfall(["Excess carrying cost", "Obsolescence exposure", "Markdown exposure", "Expected stock-out cost", "Expedite cost", "Transfer cost", "Total exposure"],
                          [fin["excess_carrying_cost"], fin["obsolescence_exposure"], fin["markdown_exposure"], fin["cost_of_stockout"], fin["expedite_cost"], fin["transfer_cost"],
                           fin["excess_carrying_cost"] + fin["obsolescence_exposure"] + fin["markdown_exposure"] + fin["cost_of_stockout"] + fin["expedite_cost"] + fin["transfer_cost"]], height=320)
    by_fam = defaultdict(lambda: defaultdict(float))
    for i, r in snap.rows(f):
        g = by_fam[i.item.get("family_code") or "?"]
        g["value"] += r.on_hand_value
        g["excess"] += r.excess_value
        g["carry"] += r.on_hand_value * snap.cfg.holding_rate
        g["obs"] += r.obsolescence_exposure
        g["lost"] += r.lost_sales_value
    top = sorted(by_fam.items(), key=lambda kv: -kv[1]["value"])[:14]
    wc_fig = charts.grouped_bars([k for k, _ in top], [{"name": "Inventory value", "y": [v["value"] for _, v in top]}, {"name": "Excess", "y": [v["excess"] for _, v in top]}], height=300, ytitle="Value")
    rows = [{"family": k, **v} for k, v in sorted(by_fam.items(), key=lambda kv: -kv[1]["value"])]
    # cost comparison for a chosen SKU-location
    cmp_rows, cmp_key = [], None
    sku, loc = request.args.get("sku"), request.args.get("loc")
    if sku and loc:
        key = next((k for k, i in snap.inputs.items() if i.sku == sku and i.loc_code == loc), None)
        if key:
            res = opt.generate_options(snap, key)
            cmp_key = (sku, loc)
            cur = snap.results[key].lost_margin
            for o in res["options"]:
                cmp_rows.append({"option": o["label"], "type": o["type"], "current": cur, "proposed": o["incremental_cost"] + o["after"]["lost_margin"], "delta": o["incremental_cost"] + o["after"]["lost_margin"] - cur,
                                 "cash": o["cash_outlay"], "inv": o["after"]["on_hand_value"] - snap.results[key].on_hand_value, "feasible": "FEASIBLE" if o["feasible"] else "BLOCKED"})
    return render_template("finance/financial.html", empty=False, fin=fin, wf=wf, wc_fig=wc_fig, rows=rows, cmp_rows=cmp_rows, cmp_key=cmp_key, rate=snap.cfg.holding_rate)


@bp.route("/sustainability")
def sustainability():
    if not H.has_data():
        return render_template("finance/sustainability.html", empty=True)
    snap = H.snap()
    co = financial_service.carbon_overview(snap)
    mode_fig = charts.bar_chart(list(co["by_mode"]) or ["none"], list(co["by_mode"].values()) or [0], height=260, ytitle="kg CO2e (estimate)", color="@s3")
    # comparison tool
    w, d, m = H.fnum(request.args.get("kg"), 5000.0), H.fnum(request.args.get("km"), 1400.0), (request.args.get("mode") or "ROAD").upper()
    cmp_rows = carbon_service.compare_modes(w, d, m if m in ("ROAD", "RAIL", "SEA", "AIR") else "ROAD")
    # transfer vs alternative source vs expedite for a real shortage? keep generic distances
    tr = {"option": "Inter-node transfer (road)", "mode": "ROAD", "cost": carbon_service.freight_cost(w, d * 0.35, "ROAD"), "co2e_kg": carbon_service.emissions_kg(w, d * 0.35, "ROAD"), "transit_days": carbon_service.transit_days(d * 0.35, "ROAD")}
    alt = {"option": "Alternate source (air from farther supplier)", "mode": "AIR", "cost": carbon_service.freight_cost(w, d * 3.2, "AIR"), "co2e_kg": carbon_service.emissions_kg(w, d * 3.2, "AIR"), "transit_days": carbon_service.transit_days(d * 3.2, "AIR")}
    cmp_rows = cmp_rows + [tr, alt]
    cmp_fig = charts.bar_chart([r["option"] for r in cmp_rows], [r["co2e_kg"] for r in cmp_rows], height=260, horizontal=True, ytitle="kg CO2e (estimate)", color="@s3")
    assets = ReusableAsset.query.all()
    pool = defaultdict(lambda: {"available": 0, "transit": 0, "repair": 0, "lost": 0, "value": 0.0, "nodes": set()})
    for a in assets:
        p = pool[a.asset_type]
        p["available"] += a.qty_available
        p["transit"] += a.qty_in_transit
        p["repair"] += a.qty_repair
        p["lost"] += a.qty_lost
        p["value"] += (a.qty_available + a.qty_in_transit + a.qty_repair) * (a.unit_value or 0)
        p["nodes"].add(a.location.code if a.location else "?")
    prow = [{"type": k, "avail": v["available"], "transit": v["transit"], "repair": v["repair"], "lost": v["lost"], "loss": v["lost"] / max(v["available"] + v["transit"] + v["repair"] + v["lost"], 1), "value": v["value"], "nodes": len(v["nodes"])} for k, v in pool.items()]
    arows = [{"pool": a.pool_code, "type": a.asset_type, "node": a.location.code if a.location else "", "avail": a.qty_available, "transit": a.qty_in_transit, "repair": a.qty_repair, "lost": a.qty_lost, "cond": a.condition} for a in assets]
    disp = defaultdict(float)
    for r in ReturnRecord.query.all():
        disp[r.disposition or "PENDING"] += r.qty
    ret_fig = charts.bar_chart(list(disp), list(disp.values()), height=240, ytitle="Units", color="@s6")
    return render_template("finance/sustainability.html", empty=False, co=co, mode_fig=mode_fig, cmp_rows=cmp_rows, cmp_fig=cmp_fig, prow=prow, arows=arows, ret_fig=ret_fig, q={"kg": w, "km": d, "mode": m},
                           factors=S.get("carbon.factors"), rates=S.get("freight.rate_per_tkm"))
