from flask import Blueprint, render_template

from app.models import Item, Location, Supplier, Customer, PurchaseOrder, TransferOrder, SalesOrder
from app.services import risk_service

bp = Blueprint("master_data", __name__, url_prefix="/master-data")


@bp.route("/")
def home():
    quality = _master_data_quality_checks()
    return render_template("master_data/home.html", quality=quality,
                            counts={"sku_count": Item.query.count(), "locations": Location.query.count(),
                                    "suppliers": Supplier.query.count(), "customers": Customer.query.count()})


@bp.route("/items")
def items():
    return render_template("master_data/items.html", items=Item.query.order_by(Item.sku).all())


@bp.route("/locations")
def locations():
    return render_template("master_data/locations.html", locations=Location.query.all())


@bp.route("/suppliers")
def suppliers():
    rows = [{"supplier": s, "risk": risk_service.supplier_risk_score(s)} for s in Supplier.query.all()]
    return render_template("master_data/suppliers.html", rows=rows)


@bp.route("/customers")
def customers():
    return render_template("master_data/customers.html", customers=Customer.query.all())


@bp.route("/orders")
def orders():
    pos = PurchaseOrder.query.order_by(PurchaseOrder.order_date.desc()).limit(100).all()
    tos = TransferOrder.query.order_by(TransferOrder.created_at.desc()).limit(100).all()
    sos = SalesOrder.query.order_by(SalesOrder.order_date.desc()).limit(100).all()
    return render_template("master_data/orders.html", pos=pos, tos=tos, sos=sos)


def _master_data_quality_checks():
    issues = []
    for item in Item.query.all():
        if not item.primary_supplier_id:
            issues.append({"entity": "Item", "id": item.sku, "issue": "Missing primary supplier"})
        if not item.moq or item.moq <= 0:
            issues.append({"entity": "Item", "id": item.sku, "issue": "Missing/invalid MOQ"})
        if not item.unit_cost or item.unit_cost <= 0:
            issues.append({"entity": "Item", "id": item.sku, "issue": "Missing/invalid unit cost"})
    for supplier in Supplier.query.all():
        if not supplier.lead_time_mean_days:
            issues.append({"entity": "Supplier", "id": supplier.code, "issue": "Missing lead time"})
    skus = [i.sku for i in Item.query.all()]
    dupes = {s for s in skus if skus.count(s) > 1}
    for d in dupes:
        issues.append({"entity": "Item", "id": d, "issue": "Duplicate SKU"})

    total_checks = max(len(Item.query.all()) * 3 + len(Supplier.query.all()), 1)
    score = round(max(0, 100 - (len(issues) / total_checks * 100)), 1)
    return {"issues": issues, "score": score}
