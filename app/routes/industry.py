"""Industry mode: profiles configure the platform; domain views feed each vertical."""
from __future__ import annotations

from flask import Blueprint, render_template, request

from ..extensions import db
from ..models import IndustryProfile
from ..services import industry_service as ind, settings_service as S
from ..utils.security import require
from . import helpers as H

bp = Blueprint("industry", __name__)


@bp.route("/industry")
def home():
    active = ind.active_profile()
    profs = IndustryProfile.query.order_by(IndustryProfile.name).all()
    if not H.has_data():
        return render_template("industry/home.html", active=active, profs=profs, view=None, data={})
    snap = H.snap()
    view = request.args.get("view") or {"AUTOMOTIVE": "automotive", "PHARMA": "pharma", "RETAIL_FMCG": "retail", "HIGH_TECH": "hightech", "SPARE_PARTS": "spare", "MANUFACTURING": "manufacturing"}.get(active.code if active else "", "automotive")
    data = {}
    if view == "automotive":
        data = {"eco": ind.eco_analysis(snap), "shutdown": ind.plant_shutdown_risk(snap), "line_side": ind.line_side(snap)}
    elif view == "pharma":
        data = {"batches": ind.batch_view(snap)}
    elif view == "retail":
        data = {"omni": ind.omnichannel(snap)}
    elif view == "hightech":
        data = {"life": ind.lifecycle(snap)}
    elif view == "spare":
        data = {"spare": ind.intermittent(snap)}
    elif view == "manufacturing":
        data = {"feas": ind.production_feasibility(snap)}
    return render_template("industry/home.html", active=active, profs=profs, view=view, data=data)


@bp.route("/industry/activate", methods=["POST"])
@require("configure")
def activate():
    try:
        p = ind.activate(request.form.get("code", ""), H.actor())
        db.session.commit()
        H.ok(f"Industry mode → {p.name}. Terminology, KPIs, rules and policy overrides now follow this profile; the core engine is unchanged.")
    except ValueError as e:
        H.err(str(e))
    return H.back("industry.home")
