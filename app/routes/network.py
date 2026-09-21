import json
from flask import Blueprint, render_template, request

from app.models import Location, Item, PurchaseOrder, TransferOrder
from app.services import inventory_service, demand_stats, risk_service

bp = Blueprint("network", __name__, url_prefix="/network")


@bp.route("/")
def network_map():
    nodes = []
    for loc in Location.query.all():
        kpis = inventory_service.status_quantities(location_id=loc.id)
        on_hand = kpis.get("ON_HAND", 0.0)
        nodes.append({
            "id": loc.code, "name": loc.name, "type": loc.node_type, "region": loc.region,
            "lat": loc.latitude, "lon": loc.longitude, "on_hand": on_hand,
            "capacity": loc.storage_capacity_units,
            "utilization_pct": round(on_hand / loc.storage_capacity_units * 100, 1) if loc.storage_capacity_units else None,
        })

    edges = []
    for loc in Location.query.filter(Location.parent_location_id.isnot(None)).all():
        parent = Location.query.get(loc.parent_location_id)
        if parent:
            edges.append({"source": parent.code, "target": loc.code, "type": "distribution"})

    # supplier -> destination edges from open POs
    seen = set()
    for po in PurchaseOrder.query.filter(PurchaseOrder.status.in_(["OPEN", "IN_TRANSIT", "DELAYED"])).all():
        key = (po.supplier.code if po.supplier else None, po.destination.code if po.destination else None)
        if key not in seen and all(key):
            seen.add(key)
            edges.append({"source": f"SUP-{key[0]}", "target": key[1], "type": "supply",
                          "delayed": po.status == "DELAYED"})
    for sup in {po.supplier for po in PurchaseOrder.query.all() if po.supplier}:
        nodes.append({"id": f"SUP-{sup.code}", "name": sup.name, "type": "supplier", "region": sup.country,
                      "lat": None, "lon": None, "on_hand": None, "capacity": None, "utilization_pct": None})

    return render_template("network/map.html", nodes_json=json.dumps(nodes), edges_json=json.dumps(edges))


@bp.route("/heatmap")
def heatmap():
    metric = request.args.get("metric", "days_of_supply")
    items = Item.query.filter_by(is_active=True).limit(30).all()
    locations = Location.query.filter(Location.node_type.in_(["dc", "store"])).all()

    z = []
    for item in items:
        row = []
        for loc in locations:
            avail = inventory_service.available_quantity(item.id, loc.id)
            d = demand_stats.daily_demand_stats(item.id, loc.id)
            if metric == "days_of_supply":
                val = round(avail / d["avg_demand_daily"], 1) if d["avg_demand_daily"] > 0 else None
            elif metric == "value":
                val = round(avail * item.unit_cost, 0)
            elif metric == "stockout_risk":
                risk = risk_service.stockout_risk(item.id, loc.id) if d["avg_demand_daily"] > 0 else None
                val = risk["probability_pct"] if risk else 0
            else:
                val = avail
            row.append(val)
        z.append(row)

    return render_template("network/heatmap.html", metric=metric,
                            x_labels=json.dumps([l.code for l in locations]),
                            y_labels=json.dumps([i.sku for i in items]), z_values=json.dumps(z))
