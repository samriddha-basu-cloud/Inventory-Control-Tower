from flask import Blueprint, redirect, url_for, flash, jsonify, request
from app.extensions import db
from app.models import Item, Location, Supplier, Customer, PurchaseOrder

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    return redirect(url_for("dashboard.control_tower"))


@bp.route("/demo/launch", methods=["POST"])
def launch_demo():
    from app.services.demo_data_service import load_demo_data
    db.create_all()
    summary = load_demo_data(reset=True)

    from app.services.optimization_service import recompute_safety_stock
    recompute_safety_stock()
    from app.services.alert_service import generate_alerts
    generate_alerts()

    flash(f"Demo network loaded: {summary['items']} SKUs across {summary['locations']} locations. "
          f"Disruption injected at {summary['disruption_supplier']} affecting "
          f"{', '.join(summary['disrupted_skus'])}.", "success")
    return redirect(url_for("dashboard.control_tower"))


@bp.route("/search")
def global_search():
    q = (request.args.get("q") or "").strip()
    results = []
    if q:
        for item in Item.query.filter(Item.sku.ilike(f"%{q}%") | Item.name.ilike(f"%{q}%")).limit(8):
            results.append({"type": "Item", "label": f"{item.sku} - {item.name}",
                             "url": url_for("inventory.item_detail", sku=item.sku)})
        for loc in Location.query.filter(Location.code.ilike(f"%{q}%") | Location.name.ilike(f"%{q}%")).limit(5):
            results.append({"type": "Location", "label": f"{loc.code} - {loc.name}",
                             "url": url_for("network.network_map")})
        for sup in Supplier.query.filter(Supplier.name.ilike(f"%{q}%")).limit(5):
            results.append({"type": "Supplier", "label": sup.name, "url": url_for("master_data.suppliers")})
        for po in PurchaseOrder.query.filter(PurchaseOrder.po_number.ilike(f"%{q}%")).limit(5):
            results.append({"type": "Purchase Order", "label": po.po_number, "url": url_for("master_data.orders")})
    return jsonify(results)
