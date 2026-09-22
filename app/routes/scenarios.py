"""Scenario Lab, Digital Twin and S&OP."""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, redirect, render_template, request, url_for

from ..extensions import db
from ..models import InventoryBalance, Scenario
from ..services import jobs, simulation_service as sim, sop_service, audit_service
from ..services.simulation_service import CHANGE_TYPES
from ..utils import charts
from ..utils.security import require
from . import helpers as H

bp = Blueprint("scenarios", __name__)
METRIC_FMT = {"inventory_value_avg": ",.0f", "service_level": ".1%", "stockout_probability": ".1%", "pairs_at_risk": ",.0f", "working_capital": ",.0f", "carrying_cost": ",.0f", "expedite_cost": ",.0f",
              "carbon_kg": ",.0f", "revenue_risk": ",.0f", "production_risk": ",.0f", "lost_sales": ",.0f"}
METRIC_LABEL = {"inventory_value_avg": "Avg inventory value", "service_level": "Service level", "stockout_probability": "Period stock-out rate", "pairs_at_risk": "SKU-locations at risk",
                "working_capital": "Working capital", "carrying_cost": "Carrying cost", "expedite_cost": "Expedite/transfer cost", "carbon_kg": "Carbon (kg CO2e, est.)", "revenue_risk": "Revenue risk",
                "production_risk": "Production risk", "lost_sales": "Lost sales"}


def _parse_changes(form) -> list[dict]:
    out = []
    for i in (1, 2, 3):
        t = form.get(f"c{i}_type")
        if not t or t not in CHANGE_TYPES:
            continue
        params = {}
        for name, typ, _ in CHANGE_TYPES[t]["params"]:
            v = form.get(f"c{i}_{t}_{name}")
            if v in (None, ""):
                continue
            params[name] = float(v) if typ == "number" else v.strip()[:80]
        out.append({"type": t, "params": params})
    return out


@bp.route("/scenarios", methods=["GET", "POST"])
def lab():
    if request.method == "POST":
        from ..utils.security import has_perm
        if not has_perm("run_scenarios"):
            return render_template("errors/error.html", code=403, message="Your role cannot run scenarios."), 403
        changes = _parse_changes(request.form)
        try:
            job = jobs.submit("scenario", _run, name=(request.form.get("name") or "Untitled scenario")[:150], changes=changes, description=(request.form.get("description") or "")[:500],
                              horizon_days=int(request.form.get("horizon") or 91), step_days=int(request.form.get("step") or 7), runs=min(int(request.form.get("runs") or 30), 200),
                              seed=int(request.form.get("seed") or 42), created_by=H.actor())
            if job.status == "DONE" and job.result and job.result.get("id"):
                H.ok("Scenario simulated on the digital twin - production data was not modified.")
                return redirect(url_for("scenarios.scenario_detail", scenario_id=job.result["id"]))
            H.err(f"Scenario failed: {job.error}")
        except ValueError as e:
            H.err(str(e))
    scen = Scenario.query.order_by(Scenario.id.desc()).limit(60).all()
    rows = [{"id": s.id, "no": s.scenario_no, "name": s.name, "by": s.created_by, "at": s.created_at, "changes": ", ".join(CHANGE_TYPES[c["type"]]["label"] for c in (s.changes or [])) or "Baseline (no changes)",
             "step": f"{s.time_step_days} d", "horizon": s.horizon_days, "sl": next((d["delta"] for d in (s.results or {}).get("deltas", []) if d["metric"] == "service_level"), None),
             "rr": next((d["delta"] for d in (s.results or {}).get("deltas", []) if d["metric"] == "revenue_risk"), None)} for s in scen]
    return render_template("scenarios/lab.html", rows=rows, types=CHANGE_TYPES, snap_ok=H.has_data())


def _run(**kw):
    row = sim.run_scenario(**kw)
    db.session.commit()
    return {"id": row.id}


@bp.route("/scenarios/<int:scenario_id>")
def scenario_detail(scenario_id):
    s = Scenario.query.get_or_404(scenario_id)
    res = s.results or {}
    deltas = res.get("deltas", [])
    figs = [charts.compare_bars(METRIC_LABEL[d["metric"]], d["baseline"], d["scenario"], METRIC_FMT[d["metric"]]) for d in deltas]
    series = None
    if res:
        b, sc = res["baseline"]["series"], res["scenario"]["series"]
        series = charts.line_chart(b["labels"], [{"name": "Baseline inventory value", "y": b["inventory_value"], "color": "@s3"}, {"name": "Scenario inventory value", "y": sc["inventory_value"], "color": "@s1"}], height=280, ytitle="Value")
        series2 = charts.line_chart(b["labels"], [{"name": "Baseline SKU-locs with stock-out", "y": b["stockout_pairs"], "color": "@s3"}, {"name": "Scenario SKU-locs with stock-out", "y": sc["stockout_pairs"], "color": "@s2"}], height=280, ytitle="Expected count")
    else:
        series2 = None
    return render_template("scenarios/detail.html", s=s, deltas=deltas, figs=figs, series=series, series2=series2, labels=METRIC_LABEL, fmt=METRIC_FMT, types=CHANGE_TYPES,
                           by_pair=(res.get("scenario") or {}).get("by_pair", [])[:25])


@bp.route("/scenarios/compare")
def compare():
    ids = [int(x) for x in (request.args.get("ids") or "").split(",") if x.strip().isdigit()][:3]
    scen = [Scenario.query.get(i) for i in ids]
    scen = [s for s in scen if s and s.results]
    if len(scen) < 1:
        return render_template("scenarios/compare.html", scen=[], rows=[], figs=[], all=Scenario.query.filter(Scenario.results.isnot(None)).order_by(Scenario.id.desc()).limit(30).all())
    base = scen[0].results["baseline"]["metrics"]
    rows = []
    for m in sim.DELTA_METRICS:
        row = {"metric": METRIC_LABEL[m], "baseline": base[m], "key": m}
        for j, s in enumerate(scen):
            v = s.results["scenario"]["metrics"][m]
            row[f"s{j}"] = v
            row[f"d{j}"] = v - base[m]
        rows.append(row)
    figs = [charts.grouped_bars(["Baseline"] + [s.scenario_no for s in scen], [{"name": METRIC_LABEL[m], "y": [base[m]] + [s.results["scenario"]["metrics"][m] for s in scen], "color": "@s1"}], height=230,
                                fmt=METRIC_FMT[m]) for m in ("service_level", "working_capital", "revenue_risk", "carbon_kg")]
    for f in figs:
        f["layout"]["showlegend"] = False
    return render_template("scenarios/compare.html", scen=scen, rows=rows, figs=figs, all=Scenario.query.filter(Scenario.results.isnot(None)).order_by(Scenario.id.desc()).limit(30).all())


@bp.route("/digital-twin")
def twin():
    snap = H.snap() if H.has_data() else None
    comp = {}
    if snap:
        for l in snap.locs.values():
            comp[l["loc_type"]] = comp.get(l["loc_type"], 0) + 1
    checksum = round(sum(b.quantity for b in InventoryBalance.query.all()), 6)
    sandboxes = Scenario.query.order_by(Scenario.id.desc()).limit(25).all()
    return render_template("scenarios/twin.html", snap=snap, comp=comp, checksum=checksum, n_pairs=len(snap.inputs) if snap else 0, sandboxes=sandboxes, n_sup=len(snap.suppliers) if snap else 0,
                           n_cust=len(snap.customers) if snap else 0)


@bp.route("/digital-twin/sandbox", methods=["POST"])
@require("run_scenarios")
def create_sandbox():
    before = round(sum(b.quantity for b in InventoryBalance.query.all()), 6)
    row = sim.run_scenario(request.form.get("name") or "Sandbox baseline", [], description="Sandbox created from the production snapshot; no changes applied.", created_by=H.actor(),
                           horizon_days=int(request.form.get("horizon") or 91), step_days=int(request.form.get("step") or 7), runs=30)
    db.session.commit()
    after = round(sum(b.quantity for b in InventoryBalance.query.all()), 6)
    H.ok(f"Sandbox {row.scenario_no} created. Production inventory checksum before/after: {before:,.2f} / {after:,.2f} ({'unchanged ✓' if before == after else 'CHANGED ✖'}).")
    return redirect(url_for("scenarios.scenario_detail", scenario_id=row.id))


@bp.route("/sop")
def sop():
    snap = H.snap()
    s = sop_service.compute(snap)
    labels = [r["label"] for r in s["cases"]["Consensus"]["rows"]]
    cols = {"Baseline": "@s3", "Consensus": "@s1", "Upside": "@s2", "Downside": "@s7"}
    def fig(field, title, ytitle):
        f = charts.line_chart(labels, [{"name": c, "y": [r[field] for r in s["cases"][c]["rows"]], "color": cols[c]} for c in s["cases"]], height=260, ytitle=ytitle)
        return f
    return render_template("scenarios/sop.html", s=s, f_dem=fig("demand_value", "Demand", "Value"), f_inv=fig("ending_inventory", "Inventory", "Value"), f_gap=fig("gap_value", "Gap", "Value"),
                           totals=[{"case": c, **v["totals"]} for c, v in s["cases"].items()], rows=[{"case": c, **r} for c, v in s["cases"].items() for r in v["rows"]])
