"""Data Hub: uploads, REST-ready ingestion, connectors, EDI translation, event log, data latency."""
from __future__ import annotations

import json

from flask import Blueprint, Response, current_app, render_template, request, session

from ..connectors import edi as edilib
from ..connectors.base import ConnectorNotConfigured, PLANNED
from ..connectors.mock_erp import MOCK_SOURCES
from ..connectors import notifications
from ..extensions import db
from ..models import Event, SyncStatus
from ..services import event_service, ingestion_service, reconciliation_service, alert_service, jobs
from ..utils.security import UploadError, require, store_upload, validate_upload
from . import helpers as H

bp = Blueprint("datahub", __name__)


@bp.route("/data-hub")
def home():
    snap = H.snap() if H.has_data() else None
    lat = reconciliation_service.latency_report(snap) if snap else []
    events = Event.query.order_by(Event.id.desc()).limit(25).all()
    catalogue = [{"name": c.name, "system": c.system, "status": "MOCK" if c.is_mock else "NOT_CONFIGURED", "desc": c.description, "key": k} for k, c in MOCK_SOURCES.items()] + \
                [{"name": p.name, "system": p.system, "status": "NOT_CONFIGURED", "desc": p.description + " · needs: " + ", ".join(p.required), "key": None} for p in PLANNED]
    return render_template("datahub/home.html", entities=ingestion_service.SCHEMAS, lat=lat, events=events, catalogue=catalogue, samples=edilib.SAMPLES,
                           schemas={e: [(f, req) for f, (_, req, _) in s.items()] for e, s in ingestion_service.SCHEMAS.items()}, event_types=sorted(event_service.EVENT_SCHEMAS),
                           notif=notifications.status(), report=session.pop("ingest_report", None), event_schemas={k: {f: (t.__name__, r) for f, (t, r) in v.items()} for k, v in event_service.EVENT_SCHEMAS.items()})


@bp.route("/data-hub/upload", methods=["POST"])
@require("ingest")
def upload():
    entity = request.form.get("entity", "")
    commit = request.form.get("mode") == "commit"
    try:
        name, data, ext = validate_upload(request.files.get("file"))
        df = ingestion_service.read_table(data, ext, request.form.get("sheet") or None)
        rep = ingestion_service.ingest_dataframe(entity, df, commit=commit, all_or_nothing=bool(request.form.get("all_or_nothing")), source_label="UPLOAD", actor=H.actor(), file_name=name)
        if commit:
            store_upload(name, data)
            db.session.commit()
            if request.form.get("detect"):
                jobs.submit("run_detection", alert_service.run_detection, actor=H.actor())
        else:
            db.session.rollback()
        session["ingest_report"] = {"entity": entity, "file": name, "mode": "COMMITTED" if rep.committed else "PREVIEW (nothing written)", "total": rep.total, "valid": rep.valid,
                                    "created": rep.created, "updated": rep.updated, "skipped": rep.skipped, "errors": rep.errors[:60], "warnings": rep.warnings[:20], "preview": rep.preview,
                                    "mapped": rep.columns_mapped, "ignored": rep.columns_ignored, "n_errors": len(rep.errors)}
    except (UploadError, ingestion_service.IngestError) as e:
        db.session.rollback()
        H.err(str(e))
    except Exception:
        db.session.rollback()
        current_app.logger.exception("upload failed")
        H.err("The file could not be processed. Check the format and required columns.")
    return H.back("datahub.home")


@bp.route("/data-hub/template/<entity>.csv")
def template(entity):
    if entity not in ingestion_service.SCHEMAS:
        return Response("unknown entity", 404)
    return Response(",".join(ingestion_service.SCHEMAS[entity]) + "\n", mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={entity}_template.csv"})


@bp.route("/data-hub/edi", methods=["POST"])
@require("ingest")
def edi():
    text = request.form.get("edi", "")
    try:
        res = edilib.translate(text, request.form.get("partner") or None)
        applied = {"balances": 0, "events": 0, "failed": 0}
        if request.form.get("apply"):
            for r in res.records:
                if r.get("entity") == "external_balance" and r.get("sku") and r.get("location") and r.get("qty") is not None:
                    from ..models import ExternalBalance
                    from ..services import settings_service as S
                    db.session.add(ExternalBalance(system=(r["system"] or "3PL")[:20], sku_raw=r["sku"], location_raw=r["location"], quantity=r["qty"], uom=r.get("uom") or "EA", lot_no=r.get("lot"),
                                                   last_sync=S.now(), source_system="EDI"))
                    applied["balances"] += 1
            for ev in res.events:
                e = event_service.publish(ev["event_type"], ev["payload"], source=f"EDI-{res.transaction}")
                applied["events" if e.status == "PROCESSED" else "failed"] += 1
            db.session.commit()
        session["edi_result"] = {"transaction": res.transaction, "control": res.control_number, "partner": res.partner, "records": res.records, "events": res.events, "warnings": res.warnings,
                                 "applied": applied if request.form.get("apply") else None}
        H.ok(f"EDI {res.transaction} translated: {len(res.records)} records, {len(res.events)} events" + (" and applied." if request.form.get("apply") else " (preview only - nothing applied)."))
    except edilib.EdiError as e:
        H.err(str(e))
    return H.back("datahub.home")


@bp.route("/data-hub/event", methods=["POST"])
@require("ingest")
def publish_event():
    try:
        payload = json.loads(request.form.get("payload") or "{}")
    except json.JSONDecodeError as e:
        H.err(f"Invalid JSON payload: {e}")
        return H.back("datahub.home")
    ev = event_service.publish(request.form.get("event_type", ""), payload, source="UI", event_id=request.form.get("event_id") or None)
    db.session.commit()
    (H.ok if ev.status == "PROCESSED" else H.err)(f"Event {ev.event_type}: {ev.status}" + (f" - {ev.error}" if ev.error else ""))
    return H.back("datahub.home")


@bp.route("/data-hub/pull/<key>", methods=["POST"])
@require("ingest")
def pull(key):
    conn = MOCK_SOURCES.get(key)
    if not conn:
        H.err("This connector is not configured. ICT does not simulate a live connection.")
        return H.back("datahub.home")
    entity = {"mock_erp": "inventory", "mock_wms": "inventory", "mock_tms": "shipments"}[key]
    try:
        recs = conn.fetch(entity)
        mapping = conn.mapping(entity)
        if key == "mock_erp":
            mapping = mapping if entity == "inventory" else mapping
        native_map = mapping if isinstance(mapping, dict) and all(isinstance(v, str) for v in mapping.values()) else {}
        canon = [{native_map.get(k, k): v for k, v in r.items() if not k.startswith("_")} for r in recs]
        if entity == "inventory":
            for r in canon:
                r["system"] = "ERP" if key == "mock_erp" else "WMS"
            rep = ingestion_service.ingest_records("external_balances", canon, commit=True, source_label=conn.name, actor=H.actor())
            db.session.commit()
            H.ok(f"{conn.name}: pulled {len(recs)} MOCK records → {rep.created} external balances (MOCK data, not a live system).")
        else:
            H.ok(f"{conn.name}: {len(recs)} MOCK shipment milestones fetched (no canonical write for this entity).")
    except (ConnectorNotConfigured, ValueError) as e:
        H.err(str(e))
    return H.back("datahub.home")


@bp.route("/data-hub/notify-test", methods=["POST"])
@require("configure")
def notify_test():
    res = notifications.dispatch("ICT test notification", "This is a test message from Inventory Control Tower.")
    db.session.commit()
    H.ok("Notification attempts: " + "; ".join(f"{r['channel']}={r['status']}" for r in res))
    return H.back("datahub.home")
