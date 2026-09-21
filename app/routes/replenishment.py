from flask import Blueprint, render_template, request, flash, redirect, url_for

from app.services import replenishment_service

bp = Blueprint("replenishment", __name__, url_prefix="/replenishment")


@bp.route("/")
def home():
    recs = replenishment_service.generate_replenishment_recommendations(persist=False)
    triggered = [r for r in recs if r.get("trigger")]
    return render_template("replenishment/home.html", recs=triggered, total_skus=len(recs))


@bp.route("/generate", methods=["POST"])
def generate():
    recs = replenishment_service.generate_replenishment_recommendations(persist=True)
    triggered = [r for r in recs if r.get("trigger")]
    flash(f"{len(triggered)} replenishment recommendation(s) created and sent for approval.", "success")
    return redirect(url_for("replenishment.home"))
