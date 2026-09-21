from flask import Blueprint, render_template, request

from app.models import Item, Location
from app.services import allocation_service

bp = Blueprint("allocation", __name__, url_prefix="/allocation")


@bp.route("/", methods=["GET", "POST"])
def home():
    result = None
    items = Item.query.filter_by(is_active=True).order_by(Item.sku).all()
    locations = Location.query.filter(Location.node_type.in_(["dc", "store"])).all()
    selected_item = request.values.get("item_id", type=int)
    selected_location = request.values.get("location_id", type=int)
    rule = request.values.get("rule", "priority_customer")

    if selected_item and selected_location:
        result = allocation_service.allocate_item(selected_item, selected_location, rule)

    return render_template("allocation/home.html", items=items, locations=locations, result=result,
                            selected_item=selected_item, selected_location=selected_location, rule=rule)
