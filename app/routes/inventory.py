from flask import Blueprint, render_template, request, abort
import pandas as pd
from sqlalchemy import func

from app.extensions import db
from app.models import Item, Location, DemandHistory, SafetyStockPolicy, InventoryLedger
from app.services import inventory_service, demand_stats, risk_service
from app.analytics.abc_xyz import classify_abc, classify_xyz, recommend_policy
from app.analytics.aging import aging_summary, age_days, expiry_status
from datetime import date

bp = Blueprint("inventory", __name__, url_prefix="/inventory")


def _network_abc_xyz_dataframe():
    rows = []
    demand_agg = (
        db.session.query(
            DemandHistory.item_id,
            func.sum(DemandHistory.quantity).label("total_qty"),
            func.avg(DemandHistory.quantity).label("mean_qty"),
            func.max(DemandHistory.period_date).label("last_txn"),
        ).group_by(DemandHistory.item_id).all()
    )
    demand_map = {r.item_id: r for r in demand_agg}

    for item in Item.query.filter_by(is_active=True).all():
        agg = demand_map.get(item.id)
        annual_qty = (agg.total_qty * 365 / 90) if agg else 0
        stats_list = []
        for loc in Location.query.all():
            d = demand_stats.daily_demand_stats(item.id, loc.id)
            if d["n_periods"] > 0:
                stats_list.append(d)
        avg_demand = sum(s["avg_demand_daily"] for s in stats_list)
        std_demand = (sum(s["std_dev_demand_daily"] ** 2 for s in stats_list) ** 0.5) if stats_list else 0
        days_since_last = (date.today() - agg.last_txn).days if agg and agg.last_txn else None

        rows.append({
            "sku": item.sku, "item_id": item.id, "name": item.name, "category": item.category,
            "annual_consumption_value": round(annual_qty * item.unit_cost, 2),
            "mean_demand": avg_demand, "std_demand": std_demand,
            "days_since_last_txn": days_since_last,
            "unit_cost": item.unit_cost,
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = classify_abc(df)
    df = classify_xyz(df)
    df["segment"] = df["abc_class"] + df["xyz_class"]
    return df


@bp.route("/")
def item_list():
    df = _network_abc_xyz_dataframe()
    q = request.args.get("q", "").strip()
    abc_filter = request.args.get("abc")
    xyz_filter = request.args.get("xyz")
    if not df.empty:
        if q:
            df = df[df["sku"].str.contains(q, case=False) | df["name"].str.contains(q, case=False)]
        if abc_filter:
            df = df[df["abc_class"] == abc_filter]
        if xyz_filter:
            df = df[df["xyz_class"] == xyz_filter]
    records = df.to_dict("records") if not df.empty else []
    return render_template("inventory/list.html", items=records, q=q, abc_filter=abc_filter, xyz_filter=xyz_filter)


@bp.route("/abc-xyz")
def abc_xyz_matrix():
    df = _network_abc_xyz_dataframe()
    matrix = {}
    if not df.empty:
        for abc in ["A", "B", "C"]:
            for xyz in ["X", "Y", "Z"]:
                seg = f"{abc}{xyz}"
                sub = df[df["segment"] == seg]
                matrix[seg] = {
                    "sku_count": len(sub),
                    "value": round(sub["annual_consumption_value"].sum(), 2),
                    "policy": recommend_policy(seg),
                }
    total_value = df["annual_consumption_value"].sum() if not df.empty else 0
    return render_template("inventory/abc_xyz.html", matrix=matrix, total_value=total_value)


@bp.route("/aging")
def aging_view():
    rows = []
    for ledger in InventoryLedger.query.filter(InventoryLedger.status == "ON_HAND").all():
        ref_date = ledger.manufacture_date or ledger.as_of.date()
        rows.append({
            "age_days": age_days(ref_date), "quantity": ledger.quantity,
            "unit_cost": ledger.item.unit_cost if ledger.item else 0,
        })
    summary = aging_summary(rows)

    expiring = []
    for ledger in InventoryLedger.query.filter(InventoryLedger.expiry_date.isnot(None)).all():
        status = expiry_status(ledger.expiry_date)
        if status in ("NEAR_EXPIRY", "EXPIRED"):
            expiring.append({"item": ledger.item, "location": ledger.location, "batch": ledger.batch_code,
                              "expiry_date": ledger.expiry_date, "quantity": ledger.quantity, "status": status})
    return render_template("inventory/aging.html", summary=summary, expiring=expiring)


@bp.route("/<sku>")
def item_detail(sku):
    item = Item.query.filter_by(sku=sku).first()
    if not item:
        abort(404)
    location_rows = []
    for loc in Location.query.all():
        qtys = inventory_service.status_quantities(item.id, loc.id)
        if not any(qtys.values()):
            continue
        d = demand_stats.daily_demand_stats(item.id, loc.id)
        policy = SafetyStockPolicy.query.filter_by(item_id=item.id, location_id=loc.id).first()
        pos = inventory_service.inventory_position(item.id, loc.id)
        atp_result = inventory_service.atp(item.id, loc.id)
        risk = risk_service.stockout_risk(item.id, loc.id) if d["avg_demand_daily"] > 0 else None
        dos = round(qtys.get("ON_HAND", 0) / d["avg_demand_daily"], 1) if d["avg_demand_daily"] > 0 else None
        location_rows.append({
            "location": loc, "status_quantities": qtys, "demand": d, "policy": policy,
            "position": pos, "atp": atp_result, "risk": risk, "days_of_supply": dos,
        })

    history = (
        DemandHistory.query.filter_by(item_id=item.id).order_by(DemandHistory.period_date).all()
    )
    chart_data = {}
    for h in history:
        chart_data.setdefault(h.period_date.isoformat(), 0)
        chart_data[h.period_date.isoformat()] += h.quantity

    return render_template("inventory/detail.html", item=item, location_rows=location_rows,
                            chart_labels=list(chart_data.keys()), chart_values=list(chart_data.values()))
