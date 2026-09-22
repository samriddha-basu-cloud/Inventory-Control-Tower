"""Report center with CSV / XLSX / PDF exports."""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, Response, abort, render_template, request

from ..services import audit_service, report_service as RS
from ..utils.security import require
from . import helpers as H

bp = Blueprint("reports", __name__)
MIME = {"csv": "text/csv", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "pdf": "application/pdf"}


@bp.route("/reports")
def home():
    return render_template("reports/home.html", reports=[(slug, title) for slug, (title, _) in RS.REPORTS.items()])


@bp.route("/reports/view/<slug>")
def view(slug):
    if slug not in RS.REPORTS:
        abort(404)
    t = _table(slug)
    def disp(v):
        if isinstance(v, float):
            return round(v, 3)
        if hasattr(v, "isoformat"):
            return v.isoformat()[:19].replace("T", " ")
        return v
    cols = [{"k": i, "l": c} for i, c in enumerate(t.columns)]
    return render_template("reports/view.html", slug=slug, t=t, cols=cols, rows=[{i: disp(v) for i, v in enumerate(r)} for r in t.rows[:800]], truncated=len(t.rows) > 800, args=request.args)


def _table(slug):
    if slug == "scenario":
        return RS.r_scenario(H.snap(), None, int(request.args["scenario_id"]) if request.args.get("scenario_id") else None)
    return RS.build(slug, H.snap(), H.flt())


@bp.route("/reports/download/<slug>.<fmt>")
@require("export")
def download(slug, fmt):
    if slug not in RS.REPORTS or fmt not in MIME:
        abort(404)
    t = _table(slug)
    data = {"csv": RS.to_csv, "xlsx": lambda x: RS.to_xlsx([x]), "pdf": RS.to_pdf}[fmt](t)
    audit_service.log("DATA", "Report", slug, "report_exported", {"format": fmt, "rows": len(t.rows)}, actor=H.actor())
    from ..extensions import db
    db.session.commit()
    name = f"ICT_{slug}_{datetime.now():%Y%m%d}.{fmt}"
    return Response(data, mimetype=MIME[fmt], headers={"Content-Disposition": f'attachment; filename="{name}"'})


@bp.route("/reports/full-workbook.xlsx")
@require("export")
def full():
    data = RS.full_workbook(H.snap(), H.flt())
    audit_service.log("DATA", "Report", "full-workbook", "report_exported", {"format": "xlsx"}, actor=H.actor())
    from ..extensions import db
    db.session.commit()
    return Response(data, mimetype=MIME["xlsx"], headers={"Content-Disposition": f'attachment; filename="ICT_full_workbook_{datetime.now():%Y%m%d}.xlsx"'})
