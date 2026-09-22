"""Reports and exports (CSV / XLSX / PDF) plus the 18-sheet full workbook and the aging analytics table."""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ..models import (Alert, AuditLog, Allocation, BomLine, Demand, Forecast, InventoryTransaction, Item, Location, Peg, Scenario, Supplier)
from ..utils.security import csv_safe
from . import abc_xyz_service, carbon_service, expiry_service, financial_service, health_service, kpi_service, reconciliation_service, risk_service, sop_service
from . import settings_service as S
from .snapshot import PairFilter, Snapshot, get_snapshot


@dataclass
class Table:
    title: str
    columns: list[str]
    rows: list[list]
    notes: list[str] = field(default_factory=list)

    def as_dicts(self):
        return [dict(zip(self.columns, r)) for r in self.rows]


def _b(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, float):
        return round(v, 4)
    return v


# ---------------------------------------------------------------------------------------------------------- aging
def aging_table(snap: Snapshot, by: str = "sku", f: PairFilter | None = None) -> Table:
    buckets = expiry_service.aging_buckets(S.get("aging.buckets"))
    labels = [b[0] for b in buckets]
    groups: dict[str, dict] = defaultdict(lambda: {"qty": 0.0, "value": 0.0, **{l: 0.0 for l in labels}, "age_w": 0.0})
    for inp, r in snap.rows(f):
        for l in inp.lots:
            if l["qty"] <= 0 or l["state"] == "WIP":
                continue
            received = l.get("received")
            age = (snap.today - received).days if received else (inp.days_since_receipt or 0)
            key = {"sku": inp.sku, "location": inp.loc_code, "batch": l.get("lot_no") or "(no lot)", "lot": l.get("lot_no") or "(no lot)",
                   "supplier": snap.suppliers.get(l.get("supplier_id") or inp.supplier_id, {}).get("code", "(internal)"),
                   "category": inp.item.get("category") or "?"}.get(by, inp.sku)
            g = groups[key]
            v = l["qty"] * r.unit_cost
            g["qty"] += l["qty"]
            g["value"] += v
            g[expiry_service.bucket_for(age, buckets)] += v
            g["age_w"] += age * l["qty"]
    rows = []
    for key, g in sorted(groups.items(), key=lambda kv: -kv[1]["value"]):
        rows.append([key, g["qty"], g["value"], (g["age_w"] / g["qty"]) if g["qty"] else 0.0] + [g[l] for l in labels])
    return Table(f"Aging by {by} (value by age bucket)", [by.upper(), "Qty", "Value", "Avg age (d)"] + labels, rows,
                 ["Age = days since receipt of the lot/balance; buckets are configurable (aging.buckets)."])


def expiry_table(snap: Snapshot, f: PairFilter | None = None) -> Table:
    rows = []
    th = snap.cfg.expiry_thresholds
    for inp, r in snap.rows(f):
        for l in inp.lots:
            if l["qty"] <= 0 or not l.get("expiry"):
                continue
            info = expiry_service.expiry_info(l["expiry"], None, inp.item.get("shelf_life_days"), snap.today, th)
            rows.append([inp.sku, inp.loc_code, l.get("lot_no"), l["qty"], l["state"], l.get("quality"), l["expiry"], info["days_to_expiry"], info["pct_remaining"], info["status"],
                         l["qty"] * r.unit_cost])
    rows.sort(key=lambda x: x[7])
    return Table("Expiry & shelf life (FEFO order)", ["SKU", "Node", "Lot", "Qty", "State", "Quality", "Expiry", "Days to expiry", "% shelf life left", "Status", "Value"], rows,
                 ["FEFO = first-expiry-first-out: earliest expiring released stock is picked first."])


# ---------------------------------------------------------------------------------------------------------- reports
def r_inventory_health(snap, f=None) -> Table:
    kp = kpi_service.compute_all(snap, f)
    h = health_service.compute(snap, f, kp)
    rows = [[c["label"], _b(c["value"]), round(c["score"], 1) if c["score"] is not None else None, c["weight"], round(c["contribution"], 2), c["note"]] for c in h["components"]]
    rows.append(["ICT INVENTORY HEALTH INDEX", None, round(h["index"], 1), None, round(h["index"], 1), h["label"]])
    for k in kp.values():
        rows.append([f"KPI · {k['name']}", _b(k["value"]), None, None, None, k["status"]])
    return Table("Inventory Health Report", ["Component / KPI", "Measured value", "Score (0-100)", "Weight", "Contribution", "Note / status"], rows, [h["formula"]])


def r_stockout(snap, f=None) -> Table:
    rows = []
    for inp, r in sorted(snap.rows(f), key=lambda t: -t[1].stockout_prob):
        if r.d_mean <= 0:
            continue
        rows.append([inp.sku, inp.loc_code, r.pos["usable_on_hand"], r.pos["position"], r.d_mean, r.lt_plan, r.ss, r.stockout_prob, r.risk_level, r.days_to_stockout,
                     r.exp_short, r.lost_sales_value])
    return Table("Stock-out Risk Report", ["SKU", "Node", "Usable", "Position", "Demand/day", "Lead time (d)", "Safety stock", "P(stock-out)", "Risk", "Days to stock-out", "Expected shortage",
                                          "Lost sales (value)"], rows, ["Probability is over the planning lead time using demand σ and ETA uncertainty (normal approximation)."])


def r_excess(snap, f=None) -> Table:
    rows = [[i.sku, i.loc_code, r.inv_class, r.pos["on_hand"], r.days_supply, r.excess_qty, r.excess_value, r.carrying_cost_year, r.obsolescence_exposure]
            for i, r in sorted(snap.rows(f), key=lambda t: -t[1].excess_value) if r.excess_value > 0]
    return Table("Excess Inventory Report", ["SKU", "Node", "Class", "On hand", "Days of supply", "Excess qty", "Excess value", "Carrying cost / yr", "Obsolescence exposure"], rows)


def r_abc_xyz(snap, f=None) -> Table:
    rows = []
    for iid, sgm in snap.item_seg.items():
        it = snap.items[iid]
        rows.append([it["sku"], it["description"], sgm["annual_demand"], it["unit_cost"], sgm["annual_value"], sgm["cum_pct"], sgm["abc"], sgm["cv"], sgm["xyz"], sgm["fsn"], sgm["hml"], it.get("ved"), it.get("sde"), it.get("criticality")])
    rows.sort(key=lambda x: -x[4])
    return Table("ABC-XYZ Report", ["SKU", "Description", "Annual demand", "Unit cost", "Annual consumption value", "Cumulative %", "ABC", "CV", "XYZ", "FSN", "HML", "VED", "SDE", "Criticality"], rows,
                 [f"ABC thresholds {S.get('abc.thresholds')}; XYZ CV cut-offs {S.get('xyz.thresholds')}. Annual Consumption Value = annual demand × unit cost."])


def r_supplier(snap, f=None) -> Table:
    sc = risk_service.supplier_scorecards(snap)
    rows = [[s["code"], s["name"], s["country"], s["tier"], s["otif"], s["on_time"], s["in_full"], s["lt_mean"], s["lt_cv"], s["quality_rejection"], s["expedite_freq"], s["open_pos"],
             s["open_value"], round(s["risk_score"], 1), s["risk_band"]] for s in sorted(sc.values(), key=lambda x: -x["risk_score"])]
    return Table("Supplier Risk Report", ["Code", "Name", "Country", "Tier", "OTIF", "On-time", "In-full", "Mean LT (d)", "LT CV", "Quality rejection", "Expedite freq", "Open POs", "Open value", "Risk score", "Band"], rows,
                 [f"Risk score = Σ weight × component risk × 100, weights {S.get('weights.supplier_risk')}"])


def r_recon(snap, f=None) -> Table:
    rec = reconciliation_service.reconcile(snap)
    rows = [[r["system"], r["sku"], r["location"], r["source_qty"], r["ict_qty"], r["variance"], r["variance_pct"], r["last_sync"], r["status"], r["note"]] for r in rec["rows"]]
    return Table("Inventory Reconciliation Report", ["System", "SKU", "Location", "Source balance", "ICT balance", "Variance", "Variance %", "Last sync", "Status", "Note"], rows,
                 [f"Match rate {rec['match_rate']:.1%}; tolerance {rec['rules']['tolerance_pct']:.1%} or {rec['rules']['tolerance_abs']} units"])


def r_replenishment(snap, f=None) -> Table:
    rows = []
    for i, r in snap.rows(f):
        rc = r.rec
        if rc.get("qty", 0) > 0:
            rows.append([i.sku, i.loc_code, rc["policy_name"], r.pos["position"], r.rop, r.ss, r.eoq, rc["qty"], rc["order_date"], rc["arrival_date"], rc["post_order_position"], rc["risk_if_not_ordered"]["probability"],
                         rc["cost"], "; ".join(rc["notes"])])
    rows.sort(key=lambda x: -x[11])
    return Table("Replenishment Report", ["SKU", "Node", "Policy", "Position", "ROP", "Safety stock", "EOQ", "Recommended qty", "Order date", "Expected arrival", "Post-order position", "Risk if not ordered", "Cost", "Rounding notes"], rows)


def r_working_capital(snap, f=None) -> Table:
    by = defaultdict(lambda: defaultdict(float))
    for i, r in snap.rows(f):
        for dim, key in (("Location", i.loc_code), ("Family", i.item.get("family_code") or "?"), ("Industry", i.item.get("industry") or "?")):
            g = by[(dim, key)]
            g["on_hand"] += r.on_hand_value
            g["in_transit"] += r.in_transit_value
            g["on_order"] += r.on_order_value
            g["excess"] += r.excess_value
            g["carry"] += r.on_hand_value * snap.cfg.holding_rate
    rows = [[d, k, v["on_hand"], v["in_transit"], v["on_order"], v["on_hand"] + v["in_transit"], v["excess"], v["carry"]] for (d, k), v in sorted(by.items(), key=lambda kv: (kv[0][0], -kv[1]["on_hand"]))]
    return Table("Working Capital Report", ["Dimension", "Key", "On-hand value", "In-transit value", "On-order value", "Working capital", "Excess value", "Carrying cost / yr"], rows,
                 [f"Holding-cost rate {snap.cfg.holding_rate:.0%} p.a. (configurable)."])


def r_sop(snap, f=None) -> Table:
    s = sop_service.compute(snap)
    rows = []
    for case, c in s["cases"].items():
        for r in c["rows"]:
            rows.append([case, r["label"], r["demand_units"], r["demand_value"], r["cogs"], r["supply_value"], r["ending_inventory"], r["gap_value"], r["margin"], r["carrying_cost"]])
    return Table("S&OP Inventory Report", ["Case", "Month", "Demand units", "Demand value", "COGS", "Supply value", "Ending inventory", "Gap (unfunded)", "Margin", "Carrying cost"], rows, [s["note"]])


def r_scenario(snap, f=None, scenario_id: int | None = None) -> Table:
    sc = Scenario.query.get(scenario_id) if scenario_id else Scenario.query.filter(Scenario.results.isnot(None)).order_by(Scenario.id.desc()).first()
    if not sc or not sc.results:
        return Table("Scenario Report", ["Info"], [["No scenario has been run yet."]])
    rows = [[d["metric"], d["baseline"], d["scenario"], d["delta"], d["delta_pct"]] for d in sc.results["deltas"]]
    return Table(f"Scenario Report: {sc.name} ({sc.scenario_no})", ["Metric", "Baseline", "Scenario", "Delta", "Delta %"], rows,
                 [f"Base dataset: {sc.base_dataset}; changes: {sc.changes}; created by {sc.created_by}; production data not modified."])


REPORTS = {
    "inventory-health": ("Inventory Health Report", r_inventory_health), "stockout-risk": ("Stockout Risk Report", r_stockout), "excess": ("Excess Inventory Report", r_excess),
    "aging": ("Aging Report", lambda snap, f=None: aging_table(snap, "sku", f)), "abc-xyz": ("ABC-XYZ Report", r_abc_xyz), "supplier-risk": ("Supplier Risk Report", r_supplier),
    "reconciliation": ("Inventory Reconciliation Report", r_recon), "replenishment": ("Replenishment Report", r_replenishment),
    "working-capital": ("Working Capital Report", r_working_capital), "sop": ("S&OP Inventory Report", r_sop), "scenario": ("Scenario Report", r_scenario),
}


def build(slug: str, snap: Snapshot | None = None, f: PairFilter | None = None) -> Table:
    if slug not in REPORTS:
        raise KeyError(slug)
    return REPORTS[slug][1](snap or get_snapshot(), f)


# ---------------------------------------------------------------------------------------------------------- exporters
def to_csv(t: Table) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([csv_safe(c) for c in t.columns])
    for r in t.rows:
        w.writerow([csv_safe(_b(v)) for v in r])
    return ("﻿" + buf.getvalue()).encode("utf-8")


HEADER_FILL = PatternFill("solid", fgColor="1F3A5F")


def _sheet(ws, t: Table):
    ws.append([csv_safe(c) for c in t.columns])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = HEADER_FILL
        c.alignment = Alignment(vertical="center", wrap_text=True)
    for r in t.rows:
        ws.append([csv_safe(_b(v)) for v in r])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for i, col in enumerate(t.columns, 1):
        width = min(46, max(10, len(str(col)) + 2, *(len(str(r[i - 1])) + 1 for r in t.rows[:200] if i - 1 < len(r) and r[i - 1] is not None)))
        ws.column_dimensions[get_column_letter(i)].width = width


def to_xlsx(tables: list[Table], title_sheet: str | None = None) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for t in tables:
        _sheet(wb.create_sheet(t.title[:31].replace("/", "-").replace(":", "-")), t)
    if not wb.sheetnames:
        wb.create_sheet("Empty")
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def to_pdf(t: Table, max_rows: int = 400) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table as RLTable, TableStyle
    bio = io.BytesIO()
    doc = SimpleDocTemplate(bio, pagesize=landscape(A4), leftMargin=24, rightMargin=24, topMargin=28, bottomMargin=24, title=t.title)
    st = getSampleStyleSheet()
    cell = st["BodyText"].clone("cell")
    cell.fontSize, cell.leading = 6.5, 8
    els = [Paragraph(f"<b>{t.title}</b>", st["Title"]), Paragraph(f"Inventory Control Tower · generated {datetime.now():%d %b %Y %H:%M}", st["Normal"]), Spacer(1, 8)]
    def fmt(v):
        v = _b(v)
        if isinstance(v, float):
            return f"{v:,.2f}"
        if isinstance(v, int) and not isinstance(v, bool):
            return f"{v:,}"
        return "" if v is None else str(v)
    data = [[Paragraph(f"<b>{c}</b>", cell) for c in t.columns]] + [[Paragraph(fmt(v)[:60], cell) for v in r] for r in t.rows[:max_rows]]
    tbl = RLTable(data, repeatRows=1)
    tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3A5F")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                             ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CCCCCC")), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F7FA")]),
                             ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    els.append(tbl)
    if len(t.rows) > max_rows:
        els.append(Paragraph(f"<i>Showing first {max_rows} of {len(t.rows)} rows. Export CSV/XLSX for the full data.</i>", st["Italic"]))
    for n in t.notes:
        els += [Spacer(1, 4), Paragraph(n, st["Italic"])]
    doc.build(els)
    return bio.getvalue()


# ---------------------------------------------------------------------------------------------------------- full workbook
def full_workbook(snap: Snapshot | None = None, f: PairFilter | None = None) -> bytes:
    snap = snap or get_snapshot()
    T = []
    kp = kpi_service.compute_all(snap, f)
    T.append(Table("README", ["Item", "Description"], [
        ["Application", "Inventory Control Tower (ICT) - From Inventory Visibility to Autonomous Inventory Decisions."],
        ["Generated", datetime.now().isoformat(timespec="seconds")], ["As-of date", snap.today.isoformat()], ["Profile / currency", f"{S.get('industry.active')} profile; values in INR unless stated"],
        ["Sheets", "MASTER_DATA, INVENTORY, TRANSACTIONS, DEMAND, FORECAST, SAFETY_STOCK, REPLENISHMENT, ABC_XYZ, AGING, EXPIRY, SUPPLY, PEGGING, ALERTS, SCENARIOS, FINANCIAL, CARBON, AUDIT"],
        ["Definitions", "Available = usable − allocated − committed − reserved. Position = available + in-transit + on-order − backorders. See docs/inventory-methodology.md."],
        ["Data caveats", "Demo data is synthetic. CO2e figures are estimates from configured emission factors. Health index is an ICT-defined composite."],
        *[[f"KPI · {v['name']}", (f"{v['value']:.4g}" if v["value"] is not None else "n/a") + f" ({v['status']})"] for v in kp.values()]]))
    T.append(Table("MASTER_DATA", ["SKU", "Description", "Industry", "Category", "Family", "UOM", "Unit cost", "Price", "MOQ", "Multiple", "Shelf life", "Criticality", "Lot tracked", "Lifecycle", "ABC", "XYZ"],
                   [[i.sku, i.description, i.industry, i.category, i.family_code, i.uom, i.unit_cost, i.selling_price, i.moq, i.order_multiple, i.shelf_life_days, i.criticality, i.lot_tracked,
                     i.lifecycle_status, snap.item_seg.get(i.id, {}).get("abc"), snap.item_seg.get(i.id, {}).get("xyz")] for i in Item.query.order_by(Item.sku).all()]))
    T.append(Table("INVENTORY", ["SKU", "Node", "On hand", "Available", "Allocated", "In transit", "On order", "Position", "Safety stock", "ROP", "Days supply", "Risk", "Value", "Class"],
                   [[i.sku, i.loc_code, r.pos["on_hand"], r.pos["available"], r.pos["allocated"], r.pos["in_transit"], r.pos["on_order"], r.pos["position"], r.ss, r.rop, r.days_supply, r.risk_level,
                     r.on_hand_value, r.inv_class] for i, r in snap.rows(f)]))
    T.append(Table("TRANSACTIONS", ["When", "Type", "SKU", "Location", "To", "Lot", "Qty", "Before", "After", "Ref", "Actor"],
                   [[t.occurred_at, t.txn_type, t.item.sku, t.location.code, t.to_location_id, t.lot_id, t.quantity, t.on_hand_before, t.on_hand_after, f"{t.ref_type or ''} {t.ref_id or ''}", t.actor]
                    for t in InventoryTransaction.query.order_by(InventoryTransaction.occurred_at.desc()).limit(20000).all()]))
    items = {i.id: i.sku for i in Item.query.all()}
    locs = {l.id: l.code for l in Location.query.all()}
    T.append(Table("DEMAND", ["SKU", "Node", "Period start", "Qty", "Granularity"], [[items.get(d.item_id), locs.get(d.location_id), d.period_start, d.qty, d.granularity] for d in Demand.query.order_by(Demand.period_start.desc()).limit(50000).all()]))
    T.append(Table("FORECAST", ["SKU", "Node", "Period start", "Qty", "Type", "Source", "P10", "P90", "Version"],
                   [[items.get(d.item_id), locs.get(d.location_id), d.period_start, d.qty, d.forecast_type, d.source, d.p10, d.p90, d.version] for d in Forecast.query.order_by(Forecast.period_start.desc()).limit(50000).all()]))
    T.append(Table("SAFETY_STOCK", ["SKU", "Node", "Method", "Service level", "z", "Demand/day", "σ demand/day", "Lead time", "σ LT", "Safety stock", "Formula"],
                   [[i.sku, i.loc_code, r.ss_method, r.service_level, r.z, r.d_mean, r.d_std, r.lt_plan, r.lt_sigma, r.ss, r.ss_formula] for i, r in snap.rows(f)]))
    T.append(r_replenishment(snap, f))
    T[-1].title = "REPLENISHMENT"
    abc = r_abc_xyz(snap, f)
    abc.title = "ABC_XYZ"
    T.append(abc)
    ag = aging_table(snap, "sku", f)
    ag.title = "AGING"
    T.append(ag)
    ex = expiry_table(snap, f)
    ex.title = "EXPIRY"
    T.append(ex)
    sup_rows = []
    for i, r in snap.rows(f):
        for ib in i.inbound:
            sup_rows.append([ib["kind"], ib["ref"], i.sku, i.loc_code, ib["qty"], ib["eta_day"], ib.get("promised_day"), ib.get("in_transit"), ib.get("delay_days"), ib.get("delay_reason"), ib.get("lane")])
    T.append(Table("SUPPLY", ["Kind", "Ref", "SKU", "Node", "Qty", "ETA (day)", "Promised (day)", "In transit", "Delay days", "Delay reason", "Lane"], sup_rows))
    T.append(Table("PEGGING", ["SKU", "Node", "Supply", "Supply ref", "Demand", "Demand ref", "Qty", "Expected date", "Demand date", "Status"],
                   [[items.get(p.item_id), locs.get(p.location_id), p.supply_type, p.supply_ref, p.demand_type, p.demand_ref, p.qty, p.expected_date, p.demand_date, p.peg_status] for p in Peg.query.limit(50000).all()]))
    T.append(Table("ALERTS", ["Alert", "Type", "Severity", "Status", "Priority", "Title", "Owner", "Value at risk", "Revenue at risk", "Incident"],
                   [[a.alert_no, a.alert_type, a.severity, a.status, a.priority_score, a.title, a.owner, (a.impact or {}).get("value_at_risk"), (a.impact or {}).get("revenue_at_risk"),
                     a.incident.incident_no if a.incident else None] for a in Alert.query.order_by(Alert.priority_score.desc()).all()]))
    def _delta(sc, m):
        return next((d["delta"] for d in (sc.results or {}).get("deltas", []) if d["metric"] == m), None)

    T.append(Table("SCENARIOS", ["Scenario", "Name", "Created by", "Created", "Changes", "Service level Δ", "Working capital Δ", "Revenue risk Δ"],
                   [[s.scenario_no, s.name, s.created_by, s.created_at, str(s.changes), _delta(s, "service_level"), _delta(s, "working_capital"), _delta(s, "revenue_risk")]
                    for s in Scenario.query.all()]))
    fin = financial_service.overview(snap, f)
    T.append(Table("FINANCIAL", ["Metric", "Value"], [[k, v] for k, v in fin.items()]))
    co = financial_service.carbon_overview(snap)
    T.append(Table("CARBON", ["Metric", "Value"], [["pipeline_co2e_kg", co["pipeline_co2e_kg"]], ["expedited_co2e_kg", co["expedited_co2e_kg"]], *[[f"co2e_kg_{m}", v] for m, v in co["by_mode"].items()],
                                                   ["disclaimer", co["disclaimer"]]]))
    T.append(Table("AUDIT", ["When", "Actor", "Category", "Entity", "Id", "Event"], [[a.timestamp, a.actor, a.category, a.entity_type, a.entity_id, a.event] for a in AuditLog.query.order_by(AuditLog.id.desc()).limit(5000).all()]))
    # README last so KPI rows are complete
    return to_xlsx(T)
