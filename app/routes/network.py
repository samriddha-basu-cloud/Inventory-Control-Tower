"""Multi-echelon network view."""
from __future__ import annotations

from flask import Blueprint, render_template, request

from ..services import network_service
from ..utils import charts
from . import helpers as H

bp = Blueprint("network", __name__)


@bp.route("/network")
def view():
    if not H.has_data():
        return render_template("network/view.html", empty=True)
    snap, f = H.snap(), H.flt()
    sku = request.args.get("sku") or ""
    net = network_service.build(snap, f, sku or None)
    fig = charts.network_graph(net["nodes"], net["edges"], height=520)
    skus = sorted({snap.inputs[k].sku for k in snap.keys(f)})
    return render_template("network/view.html", empty=False, fig=fig, rows=net["rows"], sku=sku, skus=skus, n_edges=len(net["edges"]))
