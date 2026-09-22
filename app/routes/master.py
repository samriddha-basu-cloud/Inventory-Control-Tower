"""Master Data Center: items, quality score, UOM tools, hierarchical policies."""
from __future__ import annotations

from flask import Blueprint, render_template, request

from ..extensions import db
from ..models import ControlPolicy, Item, ItemUom, ReplenishmentPolicy, SafetyStockPolicy
from ..rules import policies as pol
from ..services import audit_service, master_data_service, settings_service as S
from ..utils import uom as uomlib
from ..utils.security import clean_text, require
from . import helpers as H

bp = Blueprint("master", __name__)


@bp.route("/master-data")
def home():
    snap = H.snap()
    tab = request.args.get("tab", "items")
    q = master_data_service.quality(snap)
    items = []
    for it in Item.query.order_by(Item.sku).all():
        seg = snap.item_seg.get(it.id, {})
        items.append({"sku": it.sku, "desc": it.description, "cat": it.category, "family": it.family_code, "industry": it.industry, "uom": it.uom, "cost": it.unit_cost, "price": it.selling_price,
                      "moq": it.moq, "mult": it.order_multiple, "shelf": it.shelf_life_days, "crit": it.criticality, "abc": seg.get("abc"), "xyz": seg.get("xyz"), "lot": "Yes" if it.lot_tracked else "",
                      "life": it.lifecycle_status, "haz": "Yes" if it.hazardous else "", "temp": it.temp_requirement, "origin": it.country_of_origin, "cur": it.currency})
    policies = {"Safety stock": SafetyStockPolicy.query.all(), "Replenishment": ReplenishmentPolicy.query.all(), **{f"Control · {t}": ControlPolicy.query.filter_by(policy_type=t).all() for t in sorted({p.policy_type for p in ControlPolicy.query.all()})}}
    resolved = None
    rsku, rloc = request.args.get("sku"), request.args.get("loc")
    if rsku and rloc:
        inp = next((i for i in snap.inputs.values() if i.sku == rsku and i.loc_code == rloc), None)
        if inp:
            res = {}
            for label, model in (("Safety stock", SafetyStockPolicy), ("Replenishment", ReplenishmentPolicy)):
                merged, trail = pol.resolve(model.query.all(), inp.item, inp.loc)
                res[label] = {"merged": merged, "trail": trail}
            cp = {}
            for t in sorted({p.policy_type for p in ControlPolicy.query.all()}):
                merged, trail = pol.resolve(ControlPolicy.query.filter_by(policy_type=t).all(), inp.item, inp.loc)
                cp[t] = {"merged": merged, "trail": trail}
            resolved = {"sku": rsku, "loc": rloc, "engine": res, "control": cp}
    conv = None
    if request.args.get("cq"):
        try:
            it = Item.query.filter_by(sku=request.args.get("csku")).first()
            factors = {(u.from_uom, u.to_uom): u.factor for u in ItemUom.query.filter_by(item_id=it.id)} if it else {}
            conv = {"ok": True, "result": uomlib.convert(float(request.args["cq"]), request.args.get("cfrom", "EA"), request.args.get("cto", "EA"), factors)}
        except (uomlib.UomConversionError, ValueError) as e:
            conv = {"ok": False, "error": str(e)}
    return render_template("master/home.html", tab=tab, q=q, items=items, policies=policies, levels=pol.LEVELS, resolved=resolved, conv=conv, uoms=sorted(uomlib.KNOWN_UNITS), uom_rows=master_data_service.uom_table())


@bp.route("/master-data/item/<sku>", methods=["POST"])
@require("edit_master")
def edit_item(sku):
    it = Item.query.filter_by(sku=sku).first_or_404()
    changes = {}
    for f, cast in (("description", str), ("category", str), ("unit_cost", float), ("selling_price", float), ("moq", float), ("order_multiple", float), ("criticality", str), ("uom", str)):
        v = request.form.get(f)
        if v not in (None, ""):
            try:
                val = clean_text(v) if cast is str else cast(v)
            except ValueError:
                H.err(f"Invalid value for {f}")
                return H.back("master.home")
            if f == "uom" and not uomlib.is_valid_uom(val):
                H.err(f"Invalid UOM '{val}'")
                return H.back("master.home")
            if f in ("unit_cost", "selling_price", "moq") and val < 0 or f == "order_multiple" and val <= 0:
                H.err(f"Invalid value for {f}")
                return H.back("master.home")
            if getattr(it, f) != val:
                changes[f] = [getattr(it, f), val]
                setattr(it, f, val)
    if changes:
        audit_service.log("CONFIG", "Item", sku, "master_data_edit", changes, actor=H.actor())
        S.bump_version()
        db.session.commit()
        H.ok(f"{sku} updated ({', '.join(changes)}). Change is recorded in the audit trail.")
    return H.back("master.home")
