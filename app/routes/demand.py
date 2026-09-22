"""Demand & forecast inputs, FIT contract, forecast → inventory chain."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from flask import Blueprint, render_template, request

from ..extensions import db
from ..models import Forecast, Item, Location
from ..services import audit_service, forecast_service, settings_service as S
from ..services.snapshot import week_start
from ..utils import charts
from ..utils.security import require
from ..utils.stats import bias as _bias, wape as _wape
from . import helpers as H

bp = Blueprint("demand", __name__)


@bp.route("/demand")
def home():
    if not H.has_data():
        return render_template("demand/home.html", empty=True)
    snap, f = H.snap(), H.flt()
    by_item = defaultdict(lambda: {"fc": 0.0, "act": 0.0, "pairs": 0, "with_fc": 0, "types": set(), "fa": [], "aa": [], "next4": 0.0, "last4": 0.0, "price": 0.0})
    for inp, r in snap.rows(f):
        b = by_item[inp.sku]
        b["pairs"] += 1
        b["price"] = r.price
        if inp.forecast_weekly:
            b["with_fc"] += 1
            b["types"].add(inp.forecast_type)
            b["next4"] += sum(inp.forecast_weekly[:4])
        b["last4"] += sum(inp.hist_weekly[-4:])
        for fc, a in inp.fc_pairs:
            b["fa"].append(fc)
            b["aa"].append(a)
    rows = []
    for sku, b in by_item.items():
        rows.append({"sku": sku, "pairs": b["pairs"], "cover": b["with_fc"] / b["pairs"] if b["pairs"] else 0, "types": ", ".join(sorted(t for t in b["types"] if t)) or "none", "next4": b["next4"], "last4": b["last4"],
                     "change": (b["next4"] / b["last4"] - 1) if b["last4"] else None, "bias": _bias(b["aa"], b["fa"]) if b["aa"] else None, "wape": _wape(b["aa"], b["fa"]) if b["aa"] else None,
                     "weeks": len(b["aa"])})
    rows.sort(key=lambda r: -abs(r["bias"] or 0))
    src = defaultdict(int)
    for fr in Forecast.query.with_entities(Forecast.source, Forecast.forecast_type).all():
        src[f"{fr[0]} · {fr[1]}"] += 1
    allf = [x for r in rows for x in [r["bias"]] if x is not None]
    # aggregate value chart: weekly actual vs forecast value (customer-facing pairs)
    from ..services import kpi_service
    term = kpi_service.terminal_pairs(snap)
    act, fc = defaultdict(float), defaultdict(float)
    for k in snap.keys(f):
        if k not in term:
            continue
        inp, r = snap.inputs[k], snap.results[k]
        for w, q in zip(inp.extra.get("hist_weeks", [])[-13:], inp.hist_weekly[-13:]):
            act[w] += q * r.price
        cur = week_start(snap.today)
        for i, q in enumerate(inp.forecast_weekly[:13]):
            fc[cur + timedelta(weeks=i)] += q * r.price
    keys = sorted(set(act) | set(fc))
    fig = charts.line_chart([k.isoformat() for k in keys], [{"name": "Actual demand value", "y": [act.get(k) for k in keys]}, {"name": "Effective forecast value", "y": [fc.get(k) for k in keys], "dash": "dot"}], height=300, ytitle="Value")
    return render_template("demand/home.html", empty=False, rows=rows, src=sorted(src.items()), fig=fig, contract=forecast_service.FIT_CONTRACT, precedence=S.get("engine.forecast_precedence"),
                           mean_bias=(sum(allf) / len(allf)) if allf else None, cover=sum(1 for r in rows if r["cover"] > 0) / max(len(rows), 1), skus=sorted(by_item))


@bp.route("/demand/chain")
def chain():
    snap = H.snap()
    sku, loc = request.args.get("sku"), request.args.get("loc")
    key = next((k for k, i in snap.inputs.items() if i.sku == sku and i.loc_code == loc), None)
    if not key:
        return render_template("demand/chain.html", chain=None, sku=sku, loc=loc, pct=10.0, pairs=[(i.sku, i.loc_code) for i in snap.inputs.values()][:400])
    pct = H.fnum(request.args.get("pct"), 10.0)
    return render_template("demand/chain.html", chain=forecast_service.chain(key, pct), sku=sku, loc=loc, pct=pct, pairs=[])


@bp.route("/demand/manual", methods=["POST"])
@require("ingest")
def manual():
    it, lc = Item.query.filter_by(sku=request.form.get("sku", "")).first(), Location.query.filter_by(code=request.form.get("loc", "")).first()
    qty, ps = H.fnum(request.form.get("qty")), H.fdate(request.form.get("period"))
    if not it or not lc or qty is None or qty < 0 or not ps:
        H.err("Enter a valid SKU, location, non-negative quantity and week.")
        return H.back("demand.home")
    ft = request.form.get("type", "ADJUSTED").upper()
    if ft not in forecast_service.FORECAST_TYPES:
        H.err("Forecast type must be BASELINE, CONSENSUS or ADJUSTED.")
        return H.back("demand.home")
    ps = week_start(ps)
    row = Forecast.query.filter_by(item_id=it.id, location_id=lc.id, period_start=ps, forecast_type=ft).first()
    if row:
        row.qty, row.source, row.issued_at = qty, "MANUAL", S.today()
    else:
        db.session.add(Forecast(item_id=it.id, location_id=lc.id, period_start=ps, granularity="W", qty=qty, forecast_type=ft, source="MANUAL", issued_at=S.today(), source_system="ICT"))
    audit_service.log("DATA", "Forecast", it.sku, "manual_forecast", {"location": lc.code, "week": ps.isoformat(), "qty": qty, "type": ft}, actor=H.actor())
    S.bump_version()
    db.session.commit()
    H.ok(f"Manual {ft} forecast saved for {it.sku} @ {lc.code} week of {ps}.")
    return H.back("demand.home")
