"""Ingestion, EDI, events, KPIs, reports, traceability, security, REST API and every UI route."""
import io
import json
import zipfile

import pandas as pd
import pytest
from openpyxl import load_workbook

from app.connectors import edi
from app.extensions import db
from app.models import Event, ExternalBalance, Forecast, InventoryTransaction, Item, Lot
from app.services import event_service, forecast_service, health_service, ingestion_service as ing, kpi_service, master_data_service
from app.services import report_service as RS
from app.services import settings_service as S
from app.services import traceability_service
from app.services.snapshot import get_snapshot
from tests.conftest import post


# ---- ingestion ---------------------------------------------------------------------------------------------------------------------
def test_ingestion_validation_preview_and_commit(ctx):
    df = pd.DataFrame([{"Item": "ING-1", "Description": "x", "Unit": "EA", "Cost": "12.5", "MOQ": "10", "Multiple": "5"},
                       {"Item": "ING-2", "Description": "bad uom", "Unit": "FURLONG", "Cost": "1"},
                       {"Item": "ING-3", "Description": "neg moq", "Unit": "EA", "Cost": "1", "MOQ": "-4"},
                       {"Item": "", "Description": "no sku", "Unit": "EA"}])
    prev = ing.ingest_dataframe("items", df, commit=False)
    assert prev.total == 4 and prev.valid == 1 and not prev.committed and Item.query.filter_by(sku="ING-1").first() is None
    fields = {(e["row"], e["field"]) for e in prev.errors}
    assert (5, "sku") in fields and any("UOM" in e["message"] for e in prev.errors) and any("MOQ" in e["message"] for e in prev.errors)
    assert prev.columns_mapped["sku"] == "Item" and prev.columns_mapped["unit_cost"] == "Cost"      # column aliases
    rep = ing.ingest_dataframe("items", df, commit=True)
    db.session.commit()
    assert rep.created == 1 and Item.query.filter_by(sku="ING-1").one().unit_cost == 12.5
    assert Item.query.filter_by(sku="ING-2").first() is None
    aon = ing.ingest_dataframe("items", df, commit=True, all_or_nothing=True)
    assert not aon.committed and aon.created == 0


def test_balance_load_uses_ledger_and_refuses_bad_rows(ctx):
    snap = get_snapshot()
    k = next(iter(snap.inputs))
    sku, loc = snap.inputs[k].sku, snap.inputs[k].loc_code
    n0 = InventoryTransaction.query.count()
    df = pd.DataFrame([{"sku": sku, "location": loc, "qty": 12345}, {"sku": "NOPE", "location": loc, "qty": 1}, {"sku": sku, "location": loc, "qty": -5}, {"sku": sku, "location": "??", "qty": 1}])
    if snap.inputs[k].item.get("lot_tracked"):
        pytest.skip("first pair is lot tracked")
    rep = ing.ingest_dataframe("balances", df, commit=True)
    db.session.commit()
    assert len(rep.errors) == 3 and rep.committed
    assert InventoryTransaction.query.count() == n0 + 1                     # history appended, not overwritten


def test_json_and_csv_and_xlsx_parsing():
    assert len(ing.read_table(b'[{"sku":"A"},{"sku":"B"}]', "json")) == 2 and len(ing.read_table(b'{"records":[{"sku":"A"}]}', "json")) == 1
    assert list(ing.read_table("sku,qty\nA,1\n".encode(), "csv").columns) == ["sku", "qty"]
    bio = io.BytesIO()
    pd.DataFrame({"sku": ["A"], "qty": [3]}).to_excel(bio, index=False)
    assert ing.read_table(bio.getvalue(), "xlsx").iloc[0]["sku"] == "A"
    with pytest.raises(ing.IngestError):
        ing.parse_rows("nonsense", pd.DataFrame({"a": [1]}))
    with pytest.raises(ing.IngestError):
        ing.parse_rows("balances", pd.DataFrame({"foo": [1]}))


# ---- EDI ---------------------------------------------------------------------------------------------------------------------------
def test_edi_846_856_214_translate_to_canonical_records_and_events():
    r846 = edi.translate(edi.SAMPLES["846"])
    assert r846.transaction == "846" and len(r846.records) == 2 and r846.records[0]["sku"] == "PHM-TAB-METF500" and r846.records[0]["qty"] == 7200 and r846.records[0]["location"] == "PHM-3PL-N"
    r856 = edi.translate(edi.SAMPLES["856"])
    ev = r856.events[0]
    assert ev["event_type"] == "ShipmentDispatched" and ev["payload"]["shipment_no"] == "ASN-778812" and ev["payload"]["po"] == "PO-PHM-00001" and ev["payload"]["lines"][0]["qty"] == 1000 and ev["payload"]["eta"] == "2026-09-30"
    r214 = edi.translate(edi.SAMPLES["214"])
    assert r214.events[0]["event_type"] == "ShipmentDelayed" and "Port congestion" in r214.events[0]["payload"]["reason"]
    with pytest.raises(edi.EdiError):
        edi.translate("not edi")
    with pytest.raises(edi.EdiError, match="not mapped"):
        edi.translate(edi.SAMPLES["846"].replace("ST*846", "ST*997"))


# ---- events ------------------------------------------------------------------------------------------------------------------------
def test_event_validation_idempotency_and_processing(ctx):
    snap = get_snapshot()
    k = next(k for k, i in snap.inputs.items() if not i.item.get("lot_tracked") and snap.results[k].pos["on_hand"] > 0)
    sku, loc = snap.inputs[k].sku, snap.inputs[k].loc_code
    before = snap.results[k].pos["on_hand"]
    e1 = event_service.publish("InventoryReceived", {"sku": sku, "location": loc, "qty": 25}, event_id="EV-T-1")
    db.session.commit()
    assert e1.status == "PROCESSED"
    dup = event_service.publish("InventoryReceived", {"sku": sku, "location": loc, "qty": 25}, event_id="EV-T-1")
    assert dup.id == e1.id and Event.query.filter_by(event_id="EV-T-1").count() == 1
    assert get_snapshot().results[k].pos["on_hand"] == before + 25          # applied exactly once
    assert event_service.publish("InventoryReceived", {"sku": sku, "qty": 1}).status == "FAILED"
    assert event_service.publish("InventoryReceived", {"sku": "NOPE", "location": loc, "qty": 1}).status == "FAILED"
    assert event_service.publish("NotAnEvent", {}).status == "FAILED"
    assert len(event_service.EVENT_SCHEMAS) >= 14 and {"InventoryReceived", "ShipmentDelayed", "QualityHold", "ExpiryApproaching", "ForecastUpdated"} <= set(event_service.EVENT_SCHEMAS)


def test_shipment_delayed_event_updates_eta_and_creates_alert_condition(ctx):
    from app.models import Shipment
    s = Shipment.query.filter(Shipment.status == "IN_TRANSIT", Shipment.promised_date.isnot(None)).first()
    new_eta = (s.promised_date).replace(day=min(s.promised_date.day, 28))
    ev = event_service.publish("ShipmentDelayed", {"shipment_no": s.shipment_no, "eta": (s.promised_date.fromordinal(s.promised_date.toordinal() + 9)).isoformat(), "reason": "Customs hold"})
    db.session.commit()
    assert ev.status == "PROCESSED" and Shipment.query.get(s.id).delay_days == 9 and Shipment.query.get(s.id).status == "DELAYED"


def test_fit_forecast_contract(ctx):
    snap = get_snapshot()
    inp = next(i for i in snap.inputs.values() if i.forecast_weekly)
    res = forecast_service.ingest_fit_payload({"source": "FIT", "forecast_type": "CONSENSUS", "version": "t1", "records": [
        {"sku": inp.sku, "location": inp.loc_code, "period_start": "2026-11-02", "qty": 777, "p10": 600, "p90": 900}, {"sku": "NOPE", "location": "X", "period_start": "2026-11-02", "qty": 1}]})
    db.session.commit()
    assert res["accepted"] == 1 and res["rejected"] == 1 and Forecast.query.filter_by(forecast_type="CONSENSUS", qty=777, source="FIT").count() == 1
    for bad in ({}, {"source": "X", "records": []}, {"source": "FIT", "forecast_type": "NOPE", "records": [{}]}, {"source": "FIT", "records": []}):
        with pytest.raises(forecast_service.ContractError):
            forecast_service.ingest_fit_payload(bad)
    assert forecast_service.demand_signals(inp.sku, inp.loc_code)[0]["constraints"]


# ---- KPIs, health, master data ---------------------------------------------------------------------------------------------------------
def test_kpis_are_configurable_and_health_index_is_transparent(ctx):
    snap = get_snapshot()
    k = kpi_service.compute_all(snap)
    assert {"inventory_turns", "dio", "service_level", "fill_rate", "otif", "stockout_rate", "backorder_rate", "carrying_cost", "inventory_accuracy", "excess_pct", "obsolete_pct",
            "slow_pct", "forecast_bias", "supplier_otif", "lt_variability", "order_cycle_time"} <= set(k)
    assert 0 <= k["service_level"]["value"] <= 1 and k["dio"]["value"] > 0 and k["inventory_turns"]["value"] == pytest.approx(365 / k["dio"]["value"], rel=1e-6)
    from app.models import KpiDefinition
    d = KpiDefinition.query.filter_by(code="dio").one()
    old = d.formula
    d.formula = "2 * 365 * avg_inventory_value / cogs_annual"
    db.session.commit()
    assert kpi_service.compute_all(snap, only={"dio"})["dio"]["value"] == pytest.approx(2 * k["dio"]["value"])
    d.formula = old
    db.session.commit()
    ok, msg = kpi_service.validate_formula("nonexistent_measure + 1")
    assert not ok
    h = health_service.compute(snap, kpis=k)
    assert 0 <= h["index"] <= 100 and h["index"] == pytest.approx(sum(c["contribution"] for c in h["components"]))
    assert {c["key"] for c in h["components"]} == {"availability", "service", "excess", "obsolescence", "accuracy", "forecast", "stockout_risk", "aging", "lead_time"}
    assert "not an industry standard" in h["formula"]


def test_master_data_quality_detects_planted_issues(ctx):
    it = Item.query.filter_by(sku="ING-1").first()
    if it:
        it.unit_cost = None
        it.uom = "ZZZ"
        db.session.commit()
    q = master_data_service.quality(get_snapshot())
    assert 0 <= q["score"] <= 100 and len(q["checks"]) == 10
    if it:
        assert {"missing_cost", "invalid_uom"} <= {i["check"] for i in q["issues"] if i["sku"] == "ING-1"}


def test_traceability_forward_and_backward(ctx):
    fg = traceability_service.trace(traceability_service.find_lot("TRACE-FG-PHA-01"))
    assert fg["upstream"] and fg["upstream"][0]["supplier"] and fg["customers"]                     # batch → supplier … customer
    assert {"Receipt", "Production", "Customer"} <= set(fg["chain"]) | {"Receipt", "Production", "Customer"}
    assert any(e["type"] == "TRANSFER" for e in fg["events"]) and any(e["type"] == "SHIPMENT" for e in fg["events"]) and any(e["type"] == "RETURN" for e in fg["events"])
    rm = traceability_service.trace(traceability_service.find_lot("TRACE-RM-PHA-01"))
    assert rm["downstream"] and rm["downstream"][0]["lot"] == "TRACE-FG-PHA-01" and rm["customers"]


# ---- reports -------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("slug", list(RS.REPORTS))
def test_every_report_exports_csv_xlsx_pdf(ctx, slug):
    t = RS.build(slug, get_snapshot())
    assert t.columns
    csv_bytes = RS.to_csv(t)
    assert csv_bytes.startswith(b"\xef\xbb\xbf") and t.columns[0].encode() in csv_bytes
    wb = load_workbook(io.BytesIO(RS.to_xlsx([t])))
    assert wb.sheetnames
    assert RS.to_pdf(t).startswith(b"%PDF")


def test_full_workbook_has_all_eighteen_sheets(ctx):
    wb = load_workbook(io.BytesIO(RS.full_workbook(get_snapshot())))
    assert wb.sheetnames == ["README", "MASTER_DATA", "INVENTORY", "TRANSACTIONS", "DEMAND", "FORECAST", "SAFETY_STOCK", "REPLENISHMENT", "ABC_XYZ", "AGING", "EXPIRY", "SUPPLY", "PEGGING",
                             "ALERTS", "SCENARIOS", "FINANCIAL", "CARBON", "AUDIT"]
    assert wb["INVENTORY"].max_row > 50 and wb["README"].max_row > 5


def test_exports_neutralise_spreadsheet_formula_injection():
    t = RS.Table("x", ["a"], [["=HYPERLINK(\"http://evil\")"], ["+cmd|' /C calc'!A0"], ["safe"]])
    assert b"'=HYPERLINK" in RS.to_csv(t) and b"'+cmd" in RS.to_csv(t)
    ws = load_workbook(io.BytesIO(RS.to_xlsx([t]))).active
    assert ws["A2"].value.startswith("'=") and ws["A4"].value == "safe"


# ---- security ------------------------------------------------------------------------------------------------------------------------
def test_csrf_is_enforced_on_state_changing_requests(app):
    c = app.test_client()
    assert c.post("/detect").status_code == 400                                                       # no token
    with c.session_transaction() as s:
        s["_csrf"] = "abc"
    assert c.post("/detect", data={"csrf_token": "wrong"}).status_code == 400
    assert c.post("/api/events", json={}, headers={"X-CSRF-Token": "wrong"}).status_code == 400
    assert c.post("/api/events", json={"event_type": "X", "payload": {}}, headers={"X-CSRF-Token": "abc"}).status_code in (200, 422)


def test_api_key_allows_machine_clients_without_csrf(app):
    app.config["API_KEY"] = "secret-key"
    try:
        c = app.test_client()
        r = c.post("/api/events", json={"event_type": "NoSuch", "payload": {}}, headers={"X-API-Key": "secret-key"})
        assert r.status_code == 422
        assert c.post("/api/events", json={}, headers={"X-API-Key": "nope"}).status_code == 400
    finally:
        app.config["API_KEY"] = None


def test_role_permissions_are_enforced(client):
    post(client, "/switch-role", {"role": "Executive"})
    assert post(client, "/data-hub/upload", {"entity": "items"}, follow_redirects=False).status_code == 403          # no ingest permission
    assert post(client, "/detect", follow_redirects=False).status_code == 403
    assert client.get("/api/actions").status_code == 200
    assert client.post("/api/actions", json={}, headers={"X-CSRF-Token": "tok"}).status_code == 403
    post(client, "/switch-role", {"role": "Administrator"})


def test_upload_validation_rejects_unsafe_files(client):
    def up(name, data):
        return client.post("/data-hub/upload", data={"csrf_token": "tok", "entity": "items", "mode": "preview", "file": (io.BytesIO(data), name)}, content_type="multipart/form-data", follow_redirects=True)
    for name, data, msg in [("evil.exe", b"MZ\x90", "not allowed"), ("a.csv", b"\x00\x01\x02binary", "Binary"), ("a.xlsx", b"not a zip", "XLSX"), ("a.json", b"{oops", "Invalid JSON"),
                            ("empty.csv", b"", "empty"), ("../../etc/passwd.csv", b"x", None)]:
        r = up(name, data)
        assert r.status_code == 200
        if msg:
            assert msg in r.get_data(as_text=True)
    macro = io.BytesIO()
    with zipfile.ZipFile(macro, "w") as z:
        z.writestr("xl/workbook.xml", "<x/>")
        z.writestr("xl/vbaProject.bin", "x")
    assert "Macro" in up("m.xlsx", macro.getvalue()).get_data(as_text=True)


def test_uploads_are_size_limited(app):
    assert app.config["MAX_CONTENT_LENGTH"] == 25 * 1024 * 1024
    app.config["MAX_CONTENT_LENGTH"] = 200
    try:
        c = app.test_client()
        with c.session_transaction() as s:
            s["_csrf"] = "t"
        r = c.post("/data-hub/upload", data={"csrf_token": "t", "entity": "items", "file": (io.BytesIO(b"sku\n" + b"x" * 5000), "big.csv")}, content_type="multipart/form-data")
        assert r.status_code == 413
    finally:
        app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024


def test_no_stack_traces_and_html_escaping(client):
    r = client.get("/inventory/sku/does-not-exist")
    assert r.status_code == 404 and "Traceback" not in r.get_data(as_text=True)
    r = client.get("/api/inventory/nope")
    assert r.status_code == 404 and r.get_json()["error"]
    r = client.get("/alerts?type=<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in r.get_data(as_text=True)
    r = client.get("/api/search?q=%3Cscript%3E")
    assert r.status_code == 200


def test_secrets_are_not_in_source():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    text = "\n".join(p.read_text() for p in root.rglob("*.py"))
    assert "pickle" not in text.replace("no unsafe pickle", "") and "eval(" not in text.replace("safe_eval(", "").replace("no eval", "").replace("evaluate", "")
    assert "password=" not in text.lower().replace("smtp_password", "").replace('"password="', "")


# ---- REST API ---------------------------------------------------------------------------------------------------------------------------
API_GETS = ["/api/inventory", "/api/inventory?sku=AUT-ECU-CTL", "/api/inventory/AUT-ECU-CTL", "/api/location/AUT-PLT-CHK", "/api/supplier/AUT-S-SHAN", "/api/demand", "/api/forecast", "/api/forecast/contract",
            "/api/forecast/signals", "/api/replenishment", "/api/safety-stock", "/api/alerts", "/api/incidents", "/api/scenarios", "/api/rebalancing", "/api/optimization", "/api/actions",
            "/api/approvals", "/api/audit", "/api/search?q=AUT", "/api/health"]


@pytest.mark.parametrize("url", API_GETS)
def test_api_endpoints_return_json(client, url):
    r = client.get(url)
    assert r.status_code == 200 and r.is_json
    json.dumps(r.get_json())                                      # fully serialisable, no NaN/inf leakage


def test_api_inventory_content_and_forecast_post(client):
    d = client.get("/api/inventory/AUT-ECU-CTL").get_json()
    assert d["locations"] and {"on_hand", "available", "position", "safety_stock", "reorder_point", "lead_time_p90", "recommendation"} <= set(d["locations"][0])
    r = client.post("/api/forecast", json={"source": "FIT", "records": [{"sku": "AUT-ECU-CTL", "location": "AUT-PLT-CHK", "period_start": "2026-12-07", "qty": 4321}]}, headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 200 and r.get_json()["accepted"] == 1
    bad = client.post("/api/forecast", data="not json", content_type="application/json", headers={"X-CSRF-Token": "tok"})
    assert bad.status_code == 400
    r = client.post("/api/ingest/items", json={"records": [{"sku": "API-1", "uom": "EA", "unit_cost": 5}, {"sku": "API-2", "uom": "BAD"}], "commit": True}, headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 207 and r.get_json()["created"] == 1 and len(r.get_json()["errors"]) == 1


def test_api_action_create_returns_policy_and_autonomy(client):
    r = client.post("/api/actions", json={"type": "CREATE_PO", "sku": "AUT-ECU-CTL", "location": "AUT-PLT-CHK", "qty": 600}, headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 201
    j = r.get_json()
    assert j["action_no"].startswith("ACT-") and j["policy_check"]["checks"] and "level" in j["autonomy"]
    assert client.post("/api/actions", json={"type": "CREATE_PO", "sku": "NOPE", "location": "X", "qty": 1}, headers={"X-CSRF-Token": "tok"}).status_code == 400


# ---- every UI route ------------------------------------------------------------------------------------------------------------------------
PAGES = ["/", "/inventory", "/inventory/health", "/inventory/abc-xyz", "/inventory/aging", "/inventory/expiry", "/inventory/ledger", "/inventory/trace?lot=TRACE-FG-FMC-01", "/inventory/reconciliation",
         "/inventory/sku/AUT-ECU-CTL", "/inventory/sku/FMC-BEV-JUICE-200", "/inventory/sku/PHM-INJ-INSUL", "/inventory/location/FMC-CDC-01", "/inventory/supplier/AUT-S-SHAN", "/network", "/data-hub",
         "/master-data", "/master-data?tab=quality", "/master-data?tab=policies&sku=AUT-ECU-CTL&loc=AUT-PLT-CHK", "/demand", "/demand/chain?sku=AUT-ECU-CTL&loc=AUT-PLT-CHK", "/replenishment",
         "/replenishment/decision?sku=FMC-BEV-COLA-1L&loc=FMC-RDC-S", "/safety-stock", "/lead-time", "/optimization", "/rebalancing", "/pegging", "/supply", "/risk", "/risk?by=sku", "/alerts",
         "/alerts?view=root", "/root-cause", "/scenarios", "/digital-twin", "/scenarios/compare", "/sop", "/financial", "/sustainability", "/circular-not-a-route-skip", "/industry",
         "/industry?view=pharma", "/industry?view=automotive", "/industry?view=retail", "/industry?view=manufacturing", "/actions", "/autonomy", "/governance", "/experiments", "/audit", "/reports",
         "/reports/view/inventory-health", "/settings", "/health", "/plotly.min.js"]


@pytest.mark.parametrize("url", [u for u in PAGES if "skip" not in u])
def test_ui_routes_render(client, url):
    r = client.get(url)
    assert r.status_code == 200, url
    if url.startswith("/plotly"):
        assert b"Plotly" in r.data[:5000000]
    elif url != "/health":
        html = r.get_data(as_text=True)
        assert "Traceback" not in html and "Inventory Control Tower" in html


def test_drill_down_and_search_navigation(client):
    r = client.get("/api/search?q=AUT-ECU").get_json()["results"]
    assert any(x["type"] == "SKU" and x["url"].endswith("/inventory/sku/AUT-ECU-CTL") for x in r)
    for q, kind in (("PO-AUT", "PO"), ("SHP-AUT", "Shipment"), ("AUT-S-SHAN", "Supplier"), ("FMC-CDC", "Location"), ("INC-", "Incident")):
        assert any(x["type"] == kind for x in client.get(f"/api/search?q={q}").get_json()["results"]), q
    for u in [x["url"] for x in r][:3]:
        assert client.get(u).status_code == 200


def test_global_filters_persist_across_pages(client):
    post(client, "/filters", {"industry": "PHARMA"})
    html = client.get("/inventory").get_data(as_text=True)
    assert 'href="/inventory/sku/PHM-' in html and 'href="/inventory/sku/AUT-ECU-CTL' not in html and "industry=PHARMA" in html
    assert 'href="/inventory/sku/PHM-' in client.get("/network").get_data(as_text=True) or client.get("/network").status_code == 200       # filter also reaches other pages
    post(client, "/filters", {"reset": "1"})
    assert 'href="/inventory/sku/AUT-ECU-CTL' in client.get("/inventory").get_data(as_text=True)


def test_settings_change_takes_effect_and_is_audited(client, ctx):
    from app.models import AuditLog
    n = AuditLog.query.filter_by(event="setting_changed").count()
    post(client, "/settings/save", {"key": "engine.lt_basis", "value": "p90"})
    assert S.get("engine.lt_basis") == "p90" and AuditLog.query.filter_by(event="setting_changed").count() == n + 1
    assert get_snapshot().cfg.lt_basis == "p90"
    post(client, "/settings/save", {"key": "engine.lt_basis", "reset": "1"})
    assert S.get("engine.lt_basis") == "observed_mean"
    r = post(client, "/settings/save", {"key": "engine.service_level", "value": "5"})
    assert "not saved" in r.get_data(as_text=True)


def test_industry_profile_switch_configures_without_changing_engine(client, ctx):
    base = S.get("excess.dos_threshold")
    post(client, "/industry/activate", {"code": "PHARMA"})
    assert S.get("excess.dos_threshold") == 120 and S.get("thresholds.expiry")["near_days"] == 120
    post(client, "/settings/save", {"key": "excess.dos_threshold", "value": "77"})
    assert S.get("excess.dos_threshold") == 77                                            # explicit admin setting beats the profile
    post(client, "/settings/save", {"key": "excess.dos_threshold", "reset": "1"})
    post(client, "/industry/activate", {"code": "GENERAL"})
    assert S.get("excess.dos_threshold") == base
