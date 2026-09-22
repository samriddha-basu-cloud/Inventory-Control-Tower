"""Data Hub ingestion: CSV / XLSX / JSON / REST records → canonical model, with validation and a dry-run preview.

Each entity has a schema (aliases for common source column names, required fields, parsers) and an upsert function.
Rows are validated individually; errors are reported per row/field. `preview` never writes. `commit` writes valid rows
(or nothing when `all_or_nothing`). Every load is audited and updates the SyncStatus of its source.
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

from ..extensions import db
from ..models import (Customer, Demand, ExternalBalance, Forecast, InventoryBalance, Item, ItemSupplier, LeadTimeObservation, Location, Lot,
                      PurchaseOrder, PurchaseOrderLine, SalesOrder, SalesOrderLine, Supplier, SyncStatus)
from ..utils import uom as uomlib
from . import audit_service as audit
from . import ledger_service as ledger
from . import settings_service as S

MAX_ROWS = 200_000


class IngestError(ValueError):
    pass


def _n(s) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).strip().lower()).strip("_")


def _f(v):
    if v is None or (isinstance(v, float) and v != v) or str(v).strip() == "":
        return None
    return float(str(v).replace(",", ""))


def _s(v):
    return None if v is None or (isinstance(v, float) and v != v) or str(v).strip() == "" else str(v).strip()


def _d(v):
    if v in (None, "") or (isinstance(v, float) and v != v):
        return None
    if isinstance(v, (datetime, pd.Timestamp)):
        return v.date()
    if isinstance(v, date):
        return v
    return pd.to_datetime(str(v), dayfirst=False).date()


def _b(v):
    return str(v).strip().lower() in ("1", "true", "yes", "y", "t")


# entity -> fields: name -> (parser, required, aliases)
SCHEMAS: dict[str, dict] = {
    "items": {"sku": (_s, True, ["item", "item_number", "material", "matnr", "part_number", "product"]), "description": (_s, False, ["desc", "item_description", "maktx"]),
              "category": (_s, False, []), "family": (_s, False, ["product_family"]), "industry": (_s, False, []), "uom": (_s, False, ["unit", "meins", "base_uom"]),
              "unit_cost": (_f, False, ["cost", "std_cost", "standard_cost"]), "selling_price": (_f, False, ["price"]), "moq": (_f, False, ["min_order_qty"]),
              "order_multiple": (_f, False, ["multiple", "lot_size_multiple"]), "shelf_life_days": (_f, False, ["shelf_life"]), "criticality": (_s, False, []),
              "lot_tracked": (_b, False, ["batch_managed"]), "weight_kg": (_f, False, ["weight"]), "abc_class": (_s, False, [])},
    "locations": {"code": (_s, True, ["location", "plant", "werks", "site", "node"]), "name": (_s, False, []), "type": (_s, False, ["loc_type", "location_type"]),
                  "region": (_s, False, []), "country": (_s, False, []), "city": (_s, False, []), "lat": (_f, False, ["latitude"]), "lon": (_f, False, ["longitude", "lng"]),
                  "capacity_units": (_f, False, ["capacity"]), "parent": (_s, False, ["parent_code", "source_location"]), "echelon": (_f, False, [])},
    "suppliers": {"code": (_s, True, ["supplier", "vendor", "lifnr", "supplier_code"]), "name": (_s, False, ["vendor_name"]), "country": (_s, False, []), "city": (_s, False, []),
                  "tier": (_f, False, []), "lat": (_f, False, []), "lon": (_f, False, []), "payment_terms_days": (_f, False, [])},
    "customers": {"code": (_s, True, ["customer", "kunnr", "customer_code"]), "name": (_s, False, []), "segment": (_s, False, []), "priority": (_f, False, []),
                  "region": (_s, False, []), "country": (_s, False, [])},
    "item_suppliers": {"sku": (_s, True, ["item", "material"]), "supplier": (_s, True, ["vendor"]), "lead_time_days": (_f, True, ["lead_time", "lt", "plifz"]),
                       "moq": (_f, False, []), "order_multiple": (_f, False, []), "price": (_f, False, []), "mode": (_s, False, []), "lane": (_s, False, [])},
    "balances": {"sku": (_s, True, ["item", "material", "matnr"]), "location": (_s, True, ["plant", "werks", "site", "loc"]), "qty": (_f, True, ["quantity", "on_hand", "labst", "stock"]),
                 "lot": (_s, False, ["batch", "lot_no", "charg"]), "uom": (_s, False, ["unit"]), "state": (_s, False, ["stock_status"])},
    "transactions": {"txn_id": (_s, False, ["id", "transaction_id"]), "type": (_s, True, ["txn_type", "movement_type"]), "sku": (_s, True, ["item", "material"]),
                     "location": (_s, True, ["plant", "site"]), "qty": (_f, True, ["quantity"]), "to_location": (_s, False, ["dest"]), "lot": (_s, False, ["batch"]),
                     "uom": (_s, False, []), "ref": (_s, False, ["reference"]), "reason": (_s, False, [])},
    "demand": {"sku": (_s, True, ["item", "material"]), "location": (_s, True, ["plant", "site"]), "period_start": (_d, True, ["date", "week", "period"]),
               "qty": (_f, True, ["quantity", "demand", "units"]), "granularity": (_s, False, ["freq"]), "channel": (_s, False, [])},
    "forecast": {"sku": (_s, True, ["item", "material"]), "location": (_s, True, ["plant", "site"]), "period_start": (_d, True, ["date", "week", "period"]),
                 "qty": (_f, True, ["quantity", "forecast", "units"]), "forecast_type": (_s, False, ["type"]), "source": (_s, False, []), "p10": (_f, False, []),
                 "p90": (_f, False, []), "version": (_s, False, [])},
    "purchase_orders": {"po_number": (_s, True, ["po", "ebeln", "purchase_order"]), "supplier": (_s, True, ["vendor", "lifnr"]), "location": (_s, True, ["plant", "dest", "destination"]),
                        "sku": (_s, True, ["item", "material"]), "qty": (_f, True, ["quantity"]), "promised_date": (_d, False, ["due_date", "eindt"]),
                        "eta_date": (_d, False, ["eta"]), "unit_price": (_f, False, ["price"]), "qty_received": (_f, False, [])},
    "sales_orders": {"so_number": (_s, True, ["so", "vbeln", "sales_order"]), "customer": (_s, True, ["kunnr"]), "location": (_s, True, ["plant", "ship_from"]),
                     "sku": (_s, True, ["item", "material"]), "qty": (_f, True, ["quantity"]), "requested_date": (_d, True, ["due_date", "date"]), "unit_price": (_f, False, ["price"])},
    "lead_times": {"supplier": (_s, True, ["vendor"]), "sku": (_s, False, ["item"]), "location": (_s, False, ["dest"]), "lead_time_days": (_f, True, ["lead_time", "lt"]),
                   "promised_days": (_f, False, []), "received_date": (_d, False, []), "mode": (_s, False, []), "lane": (_s, False, [])},
    "external_balances": {"system": (_s, True, ["source", "source_system"]), "sku": (_s, True, ["item", "material"]), "location": (_s, True, ["plant", "site"]),
                          "qty": (_f, True, ["quantity", "on_hand"]), "uom": (_s, False, ["unit"]), "lot": (_s, False, ["batch"]), "last_sync": (_d, False, ["as_of", "date"])},
}

SOURCE_FOR_ENTITY = {"balances": "INVENTORY", "transactions": "INVENTORY", "external_balances": "WMS", "forecast": "FORECAST", "demand": "ERP", "purchase_orders": "ERP",
                     "sales_orders": "ERP", "lead_times": "TMS", "items": "ERP", "locations": "ERP", "suppliers": "ERP", "customers": "ERP", "item_suppliers": "ERP"}


@dataclass
class Report:
    entity: str
    total: int = 0
    valid: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    committed: bool = False
    preview: list = field(default_factory=list)
    columns_mapped: dict = field(default_factory=dict)
    columns_ignored: list = field(default_factory=list)


# ---------------------------------------------------------------------------------------------------------- parsing
def read_table(data: bytes, ext: str, sheet: str | int | None = None) -> pd.DataFrame:
    if ext == "csv":
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False, nrows=MAX_ROWS + 1)
    elif ext == "xlsx":
        df = pd.read_excel(io.BytesIO(data), sheet_name=sheet or 0, dtype=str, engine="openpyxl", nrows=MAX_ROWS + 1)
        df = df.fillna("")
    elif ext == "json":
        obj = json.loads(data.decode("utf-8"))
        rows = obj.get("records") if isinstance(obj, dict) and "records" in obj else obj
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise IngestError("JSON must be a list of objects or {\"records\": [...]}")
        df = pd.DataFrame(rows).astype(object).where(lambda x: x.notna(), "")
    else:
        raise IngestError(f"Unsupported format .{ext}")
    if len(df) > MAX_ROWS:
        raise IngestError(f"File has more than {MAX_ROWS:,} rows; split it or use the streaming API.")
    return df


def map_columns(df_cols: list[str], entity: str, override: dict | None = None) -> tuple[dict, list]:
    schema = SCHEMAS[entity]
    norm = {_n(c): c for c in df_cols}
    mapping, used = {}, set()
    for field_, (_, _, aliases) in schema.items():
        if override and override.get(field_) in df_cols:
            mapping[field_] = override[field_]
            used.add(override[field_])
            continue
        for cand in [field_] + aliases:
            if _n(cand) in norm:
                mapping[field_] = norm[_n(cand)]
                used.add(norm[_n(cand)])
                break
    return mapping, [c for c in df_cols if c not in used]


def parse_rows(entity: str, df: pd.DataFrame, override: dict | None = None) -> tuple[list[dict], Report]:
    if entity not in SCHEMAS:
        raise IngestError(f"Unknown entity '{entity}'. Choose one of: {', '.join(SCHEMAS)}")
    rep = Report(entity=entity, total=len(df))
    mapping, ignored = map_columns(list(df.columns), entity, override)
    rep.columns_mapped, rep.columns_ignored = mapping, ignored
    missing_req = [f for f, (_, req, _) in SCHEMAS[entity].items() if req and f not in mapping]
    if missing_req:
        raise IngestError(f"Required column(s) not found for '{entity}': {', '.join(missing_req)}. Found: {', '.join(map(str, df.columns))}")
    rows = []
    for i, rec in enumerate(df.to_dict("records"), start=2):     # row 1 = header
        out, bad = {}, False
        for f, (parser, req, _) in SCHEMAS[entity].items():
            raw = rec.get(mapping[f]) if f in mapping else None
            try:
                v = parser(raw)
            except Exception:
                rep.errors.append({"row": i, "field": f, "message": f"cannot parse '{raw}'"})
                bad = True
                continue
            if req and v in (None, ""):
                rep.errors.append({"row": i, "field": f, "message": "required value is empty"})
                bad = True
            out[f] = v
        if not bad:
            out["_row"] = i
            rows.append(out)
    rep.valid = len(rows)
    return rows, rep


# ---------------------------------------------------------------------------------------------------------- upserts
def _err(rep: Report, row, field_, msg):
    rep.errors.append({"row": row, "field": field_, "message": msg})


def ingest_dataframe(entity: str, df: pd.DataFrame, *, commit: bool = False, all_or_nothing: bool = False, override: dict | None = None,
                     source_label: str = "UPLOAD", actor: str = "system", file_name: str | None = None) -> Report:
    rows, rep = parse_rows(entity, df, override)
    rep.preview = [{k: v for k, v in r.items() if k != "_row"} for r in rows[:8]]
    handler = HANDLERS[entity]
    sp = db.session.begin_nested()
    try:
        for r in rows:
            try:
                with db.session.begin_nested():
                    res = handler(r, rep)
                if res == "created":
                    rep.created += 1
                elif res == "updated":
                    rep.updated += 1
                else:
                    rep.skipped += 1
            except (ledger.LedgerError, uomlib.UomConversionError, ValueError) as e:
                _err(rep, r["_row"], "*", str(e))
        n_err_rows = len({e["row"] for e in rep.errors})
        rep.valid = rep.total - n_err_rows
        if not commit or (all_or_nothing and rep.errors):
            sp.rollback()
            rep.created = rep.updated = rep.skipped = 0
            if all_or_nothing and rep.errors and commit:
                rep.warnings.append("All-or-nothing mode: nothing was written because some rows failed.")
        else:
            sp.commit()
            rep.committed = True
    except Exception:
        sp.rollback()
        raise
    if rep.committed:
        st = SyncStatus.query.filter_by(source=SOURCE_FOR_ENTITY.get(entity, "ERP")).first()
        if st:
            st.last_sync, st.records, st.note = S.now(), (st.records or 0) + rep.created + rep.updated, f"{source_label} {entity}"
        audit.log("DATA", "Ingestion", entity, "ingest_committed", {"source": source_label, "file": file_name, "total": rep.total, "created": rep.created,
                                                                     "updated": rep.updated, "errors": len(rep.errors)}, actor=actor)
        S.bump_version()
    return rep


def ingest_records(entity: str, records: list[dict], **kw) -> Report:
    return ingest_dataframe(entity, pd.DataFrame(records).astype(object).where(lambda x: x.notna(), ""), **kw)


def _get(model, **kw):
    return model.query.filter_by(**kw).first()


def _item(r, rep, sku):
    it = _get(Item, sku=sku)
    if not it:
        raise ValueError(f"Missing SKU '{sku}' (create it in items first)")
    return it


def _loc(code):
    l = _get(Location, code=code)
    if not l:
        raise ValueError(f"Unknown location '{code}'")
    return l


def _upsert_generic(model, key: dict, values: dict):
    row = _get(model, **key)
    if row:
        for k, v in values.items():
            if v is not None:
                setattr(row, k, v)
        return "updated"
    db.session.add(model(**key, **{k: v for k, v in values.items() if v is not None}))
    return "created"


def h_items(r, rep):
    if r.get("uom") and not uomlib.is_valid_uom(r["uom"]):
        raise ValueError(f"Invalid UOM '{r['uom']}'")
    if r.get("moq") is not None and r["moq"] < 0:
        raise ValueError("Invalid MOQ (negative)")
    if r.get("order_multiple") is not None and r["order_multiple"] <= 0:
        raise ValueError("Invalid order multiple (must be > 0)")
    if r.get("unit_cost") is not None and r["unit_cost"] < 0:
        raise ValueError("Negative unit cost")
    vals = dict(description=r.get("description"), category=r.get("category"), family_code=r.get("family"), industry=r.get("industry"),
                uom=(r.get("uom") or "").upper() or None, unit_cost=r.get("unit_cost"), selling_price=r.get("selling_price"), moq=r.get("moq"),
                order_multiple=r.get("order_multiple"), shelf_life_days=int(r["shelf_life_days"]) if r.get("shelf_life_days") else None,
                criticality=r.get("criticality"), lot_tracked=r.get("lot_tracked") or None, weight_kg=r.get("weight_kg"), abc_class=r.get("abc_class"), source_system="UPLOAD")
    return _upsert_generic(Item, {"sku": r["sku"]}, vals)


def h_locations(r, rep):
    parent = _loc(r["parent"]) if r.get("parent") else None
    vals = dict(name=r.get("name"), loc_type=(r.get("type") or "").upper() or None, region=r.get("region"), country=r.get("country"), city=r.get("city"),
                lat=r.get("lat"), lon=r.get("lon"), capacity_units=r.get("capacity_units"), parent_id=parent.id if parent else None,
                echelon=int(r["echelon"]) if r.get("echelon") else None, source_system="UPLOAD")
    return _upsert_generic(Location, {"code": r["code"]}, vals)


def h_suppliers(r, rep):
    return _upsert_generic(Supplier, {"code": r["code"]}, dict(name=r.get("name"), country=r.get("country"), city=r.get("city"), tier=int(r["tier"]) if r.get("tier") else None,
                                                                lat=r.get("lat"), lon=r.get("lon"), payment_terms_days=int(r["payment_terms_days"]) if r.get("payment_terms_days") else None,
                                                                source_system="UPLOAD"))


def h_customers(r, rep):
    return _upsert_generic(Customer, {"code": r["code"]}, dict(name=r.get("name"), segment=r.get("segment"), priority=int(r["priority"]) if r.get("priority") else None,
                                                                region=r.get("region"), country=r.get("country"), source_system="UPLOAD"))


def h_item_suppliers(r, rep):
    it, sup = _item(r, rep, r["sku"]), _get(Supplier, code=r["supplier"])
    if not sup:
        raise ValueError(f"Unknown supplier '{r['supplier']}'")
    if r["lead_time_days"] is None or r["lead_time_days"] <= 0:
        raise ValueError("Missing/invalid lead time (must be > 0)")
    return _upsert_generic(ItemSupplier, {"item_id": it.id, "supplier_id": sup.id},
                           dict(lead_time_days=r["lead_time_days"], moq=r.get("moq"), order_multiple=r.get("order_multiple"), price=r.get("price"),
                                mode=(r.get("mode") or "").upper() or None, lane=r.get("lane"), source_system="UPLOAD"))


def _lot(it, no):
    if not no:
        return None
    l = _get(Lot, item_id=it.id, lot_no=no)
    if not l:
        l = Lot(item_id=it.id, lot_no=no, received_date=S.today(), source_system="UPLOAD")
        db.session.add(l)
        db.session.flush()
    return l


def h_balances(r, rep):
    """Set-balance semantics: posts the difference to the target as a ledger adjustment / receipt (history is preserved)."""
    it, loc = _item(r, rep, r["sku"]), _loc(r["location"])
    if r["qty"] < 0:
        raise ledger.NegativeInventoryError(f"Negative inventory not allowed ({r['qty']})")
    qty = ledger.to_base_qty(it, r["qty"], r.get("uom"))
    lot = _lot(it, r.get("lot"))
    state = (r.get("state") or "UNRESTRICTED").upper()
    b = InventoryBalance.query.filter_by(item_id=it.id, location_id=loc.id, lot_id=lot.id if lot else None, state=state).first()
    cur = b.quantity if b else 0.0
    if abs(qty - cur) < 1e-9:
        return "skipped"
    if b is None:
        ledger.post("RECEIPT", it.id, loc.id, qty, lot_id=lot.id if lot else None, state_to=state, reason="Data Hub balance load", ref_type="LOAD", source_system="UPLOAD")
        return "created"
    ledger.cycle_count(it.id, loc.id, qty, lot_id=lot.id if lot else None, state=state, reason="Data Hub balance load")
    return "updated"


def h_transactions(r, rep):
    it, loc = _item(r, rep, r["sku"]), _loc(r["location"])
    typ = r["type"].upper()
    to = _loc(r["to_location"]) if r.get("to_location") else None
    lot = _lot(it, r.get("lot"))
    try:
        ledger.post(typ, it.id, loc.id, r["qty"], lot_id=lot.id if lot else None, to_location_id=to.id if to else None, uom=r.get("uom"), txn_id=r.get("txn_id"),
                    ref_id=r.get("ref"), reason=r.get("reason") or "Data Hub load", source_system="UPLOAD")
    except ledger.DuplicateTransactionError:
        rep.warnings.append(f"row {r['_row']}: duplicate transaction id skipped")
        return "skipped"
    return "created"


def h_demand(r, rep):
    it, loc = _item(r, rep, r["sku"]), _loc(r["location"])
    if r["qty"] < 0:
        raise ValueError("Negative demand")
    g = (r.get("granularity") or "W")[:1].upper()
    row = Demand.query.filter_by(item_id=it.id, location_id=loc.id, period_start=r["period_start"], granularity=g).first()
    if row:
        row.qty = r["qty"]
        return "updated"
    db.session.add(Demand(item_id=it.id, location_id=loc.id, period_start=r["period_start"], granularity=g, qty=r["qty"], channel=r.get("channel"), source_system="UPLOAD"))
    return "created"


def h_forecast(r, rep):
    it, loc = _item(r, rep, r["sku"]), _loc(r["location"])
    ft = (r.get("forecast_type") or "BASELINE").upper()
    if ft not in ("BASELINE", "CONSENSUS", "ADJUSTED"):
        raise ValueError(f"forecast_type must be BASELINE, CONSENSUS or ADJUSTED (got {ft})")
    row = Forecast.query.filter_by(item_id=it.id, location_id=loc.id, period_start=r["period_start"], forecast_type=ft).first()
    vals = dict(qty=r["qty"], source=(r.get("source") or "UPLOAD").upper(), p10=r.get("p10"), p90=r.get("p90"), version=r.get("version"), issued_at=S.today())
    if row:
        for k, v in vals.items():
            setattr(row, k, v)
        return "updated"
    db.session.add(Forecast(item_id=it.id, location_id=loc.id, period_start=r["period_start"], granularity="W", forecast_type=ft, source_system="UPLOAD", **vals))
    return "created"


def h_purchase_orders(r, rep):
    sup, loc, it = _get(Supplier, code=r["supplier"]), _loc(r["location"]), _item(r, rep, r["sku"])
    if not sup:
        raise ValueError(f"Unknown supplier '{r['supplier']}'")
    if r["qty"] <= 0:
        raise ValueError("PO quantity must be > 0")
    if r.get("eta_date") and r["eta_date"] < S.today() - timedelta(days=3650):
        raise ValueError("Invalid ETA date")
    po = _get(PurchaseOrder, po_number=r["po_number"])
    created = False
    if not po:
        po = PurchaseOrder(po_number=r["po_number"], supplier_id=sup.id, dest_location_id=loc.id, order_date=S.today(), promised_date=r.get("promised_date"),
                           eta_date=r.get("eta_date") or r.get("promised_date"), status="OPEN", source_system="UPLOAD")
        db.session.add(po)
        db.session.flush()
        created = True
    ln = PurchaseOrderLine.query.filter_by(po_id=po.id, item_id=it.id).first()
    if ln:
        ln.qty_ordered, ln.eta_date, ln.promised_date = r["qty"], r.get("eta_date") or ln.eta_date, r.get("promised_date") or ln.promised_date
        ln.qty_received = r.get("qty_received") if r.get("qty_received") is not None else ln.qty_received
        return "updated"
    db.session.add(PurchaseOrderLine(po_id=po.id, line_no=len(po.lines) + 1, item_id=it.id, qty_ordered=r["qty"], qty_received=r.get("qty_received") or 0.0, unit_price=r.get("unit_price"),
                                     promised_date=r.get("promised_date"), eta_date=r.get("eta_date") or r.get("promised_date"), source_system="UPLOAD"))
    return "created" if created else "updated"


def h_sales_orders(r, rep):
    cust, loc, it = _get(Customer, code=r["customer"]), _loc(r["location"]), _item(r, rep, r["sku"])
    if not cust:
        raise ValueError(f"Unknown customer '{r['customer']}'")
    so = _get(SalesOrder, so_number=r["so_number"])
    if not so:
        so = SalesOrder(so_number=r["so_number"], customer_id=cust.id, ship_from_location_id=loc.id, order_date=S.today(), requested_date=r["requested_date"],
                        promised_date=r["requested_date"], priority=cust.priority or 3, status="OPEN", source_system="UPLOAD")
        db.session.add(so)
        db.session.flush()
    db.session.add(SalesOrderLine(so_id=so.id, line_no=len(so.lines) + 1, item_id=it.id, qty_ordered=r["qty"], unit_price=r.get("unit_price") or it.selling_price,
                                  requested_date=r["requested_date"], source_system="UPLOAD", status="OPEN"))
    return "created"


def h_lead_times(r, rep):
    sup = _get(Supplier, code=r["supplier"])
    if not sup:
        raise ValueError(f"Unknown supplier '{r['supplier']}'")
    if r["lead_time_days"] is None or r["lead_time_days"] <= 0:
        raise ValueError("Lead time must be > 0")
    it = _item(r, rep, r["sku"]) if r.get("sku") else None
    loc = _loc(r["location"]) if r.get("location") else None
    db.session.add(LeadTimeObservation(supplier_id=sup.id, item_id=it.id if it else None, dest_location_id=loc.id if loc else None, lead_time_days=r["lead_time_days"],
                                       promised_days=r.get("promised_days"), received_date=r.get("received_date"), mode=(r.get("mode") or "").upper() or None,
                                       lane=r.get("lane"), source_system="UPLOAD"))
    return "created"


def h_external_balances(r, rep):
    if r["system"].upper() not in ("ERP", "WMS", "3PL", "PHYSICAL"):
        raise ValueError("system must be ERP, WMS, 3PL or PHYSICAL")
    db.session.add(ExternalBalance(system=r["system"].upper(), sku_raw=r["sku"], location_raw=r["location"], quantity=r["qty"], uom=(r.get("uom") or "EA").upper(),
                                   lot_no=r.get("lot"), last_sync=datetime.combine(r["last_sync"], datetime.min.time()) if r.get("last_sync") else S.now(), source_system="UPLOAD"))
    return "created"


HANDLERS = {"items": h_items, "locations": h_locations, "suppliers": h_suppliers, "customers": h_customers, "item_suppliers": h_item_suppliers, "balances": h_balances,
            "transactions": h_transactions, "demand": h_demand, "forecast": h_forecast, "purchase_orders": h_purchase_orders, "sales_orders": h_sales_orders,
            "lead_times": h_lead_times, "external_balances": h_external_balances}

TEMPLATES = {e: [f for f in s] for e, s in SCHEMAS.items()}
