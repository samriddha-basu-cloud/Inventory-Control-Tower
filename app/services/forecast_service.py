"""Forecast integration: FIT ⇄ ICT API contract and the forecast → inventory decision chain.

ICT never forces its own forecasting engine. It consumes Baseline / Consensus / Adjusted forecasts from the Forecasting
Intelligence Tower (FIT), an ERP, an upload or manual entry, converts them to a demand *distribution*, and pushes that
through safety stock → reorder point → position → replenishment → service level.
"""
from __future__ import annotations

import copy
from datetime import date, timedelta

from ..extensions import db
from ..models import Demand, Forecast, Item, Location
from . import audit_service as audit
from . import settings_service as S
from .engine import compute_pair
from .snapshot import get_snapshot, week_start

FORECAST_TYPES = ["BASELINE", "CONSENSUS", "ADJUSTED"]
SOURCES = ["FIT", "ERP", "UPLOAD", "MANUAL", "EVENT"]

FIT_CONTRACT = {
    "name": "FIT → ICT forecast contract", "version": "1.0",
    "direction": "FIT publishes forecasts to ICT (POST /api/forecast); ICT publishes demand signals + constraints to FIT (GET /api/forecast/signals)",
    "request_example": {
        "source": "FIT", "version": "FIT-2026.09", "model": "ETS-ML-ensemble", "forecast_type": "BASELINE", "granularity": "W", "issued_at": "2026-09-21",
        "records": [{"sku": "FMC-BEV-COLA-1L", "location": "FMC-RDC-S", "period_start": "2026-09-28", "qty": 9800, "p10": 8100, "p90": 11700}],
    },
    "fields": {
        "source": "FIT | ERP | UPLOAD | MANUAL (required)", "forecast_type": "BASELINE | CONSENSUS | ADJUSTED (default BASELINE)", "granularity": "W (weekly buckets; D/M are aggregated by the sender)",
        "records[].sku": "must exist in ICT master data", "records[].location": "must exist in ICT master data", "records[].period_start": "ISO date (any weekday; aligned to Monday)",
        "records[].qty": ">= 0, in the item base UOM", "records[].p10/p90": "optional quantiles; used to derive demand σ if forecast-error history is thin",
    },
    "auth": "X-API-Key header (env API_KEY) for machine clients, or an authenticated session with the 'ingest' permission",
    "precedence": "ICT uses ADJUSTED > CONSENSUS > BASELINE per period (configurable: engine.forecast_precedence)",
    "signals_response_example": {"sku": "...", "location": "...", "weekly_actuals": [{"period_start": "2026-08-31", "qty": 9100, "censored": False}],
                                 "stockout_censored_weeks": ["2026-09-07"], "constraints": {"moq": 4800, "lead_time_p90_days": 9}},
}


class ContractError(ValueError):
    pass


def ingest_fit_payload(payload: dict, actor: str = "FIT") -> dict:
    if not isinstance(payload, dict):
        raise ContractError("Payload must be a JSON object")
    source = str(payload.get("source", "")).upper()
    if source not in SOURCES:
        raise ContractError(f"'source' must be one of {SOURCES}")
    ftype = str(payload.get("forecast_type", "BASELINE")).upper()
    if ftype not in FORECAST_TYPES:
        raise ContractError(f"'forecast_type' must be one of {FORECAST_TYPES}")
    recs = payload.get("records")
    if not isinstance(recs, list) or not recs:
        raise ContractError("'records' must be a non-empty list")
    if len(recs) > 100000:
        raise ContractError("Too many records in one call (max 100,000)")
    issued = date.fromisoformat(payload["issued_at"]) if payload.get("issued_at") else S.today()
    skus = {i.sku: i.id for i in Item.query.all()}
    locs = {l.code: l.id for l in Location.query.all()}
    created = updated = 0
    errors = []
    for n, r in enumerate(recs):
        try:
            iid, lid = skus.get(r.get("sku")), locs.get(r.get("location"))
            if iid is None:
                raise ValueError(f"unknown sku '{r.get('sku')}'")
            if lid is None:
                raise ValueError(f"unknown location '{r.get('location')}'")
            q = float(r["qty"])
            if q < 0:
                raise ValueError("negative qty")
            ps = week_start(date.fromisoformat(str(r["period_start"])[:10]))
        except (KeyError, ValueError, TypeError) as e:
            errors.append({"index": n, "error": str(e)})
            continue
        row = Forecast.query.filter_by(item_id=iid, location_id=lid, period_start=ps, forecast_type=ftype).first()
        vals = dict(qty=q, source=source, p10=r.get("p10"), p90=r.get("p90"), version=payload.get("version"), model_name=payload.get("model"), issued_at=issued)
        if row:
            for k, v in vals.items():
                setattr(row, k, v)
            updated += 1
        else:
            db.session.add(Forecast(item_id=iid, location_id=lid, period_start=ps, granularity="W", forecast_type=ftype, source_system=source, **vals))
            created += 1
    audit.log("DATA", "Forecast", payload.get("version") or source, "forecast_ingested", {"source": source, "type": ftype, "created": created, "updated": updated,
                                                                                         "errors": len(errors)}, actor=actor)
    from ..models import SyncStatus
    st = SyncStatus.query.filter_by(source="FORECAST").first()
    if st:
        st.last_sync, st.records, st.note = S.now(), (st.records or 0) + created + updated, f"{source} {ftype}"
    S.bump_version()
    return {"accepted": created + updated, "created": created, "updated": updated, "rejected": len(errors), "errors": errors[:50]}


def demand_signals(sku: str | None = None, location: str | None = None, weeks: int = 26) -> list[dict]:
    """ICT → FIT: actuals plus the constraints FIT should know about (stock-out censoring, MOQ, lead-time)."""
    snap = get_snapshot()
    out = []
    for k, inp in snap.inputs.items():
        if (sku and inp.sku != sku) or (location and inp.loc_code != location):
            continue
        r = snap.results[k]
        wks = inp.extra.get("hist_weeks", [])[-weeks:]
        hist = inp.hist_weekly[-weeks:]
        out.append({"sku": inp.sku, "location": inp.loc_code, "weekly_actuals": [{"period_start": w.isoformat(), "qty": q} for w, q in zip(wks, hist)],
                    "stockout_censored_weeks": [w.isoformat() for w, q in zip(wks, hist) if r.pos["usable_on_hand"] <= 0 and q == 0][:4],
                    "constraints": {"moq": inp.moq, "order_multiple": inp.multiple, "lead_time_p90_days": r.lt_p90, "lead_time_static_days": r.lt_static},
                    "current_forecast_type": inp.forecast_type})
    return out


def chain(key: tuple, change_pct: float) -> dict:
    """See how a forecast change flows through: forecast → demand distribution → SS → ROP → position → replenishment → service."""
    snap = get_snapshot()
    inp, r0 = snap.inputs[key], snap.results[key]
    i2 = copy.deepcopy(inp)
    i2.forecast_weekly = [x * (1 + change_pct / 100.0) for x in i2.forecast_weekly]
    r1 = compute_pair(i2, snap.cfg, with_projection=False)

    def row(label, a, b, unit=""):
        return {"step": label, "before": a, "after": b, "delta": (b - a) if (a is not None and b is not None) else None, "unit": unit}

    rows = [
        row("Forecast (avg next lead-time window)", r0.d_mean * (r0.lt_plan + 7) / max(r0.lt_plan + 7, 1) * 7, r1.d_mean * 7, "units/week"),
        row("Demand distribution μ (per day)", r0.d_mean, r1.d_mean, "units/day"),
        row("Demand distribution σ (per day)", r0.d_std, r1.d_std, "units/day"),
        row("Safety stock", r0.ss, r1.ss, "units"),
        row("Reorder point", r0.rop, r1.rop, "units"),
        row("Inventory position", r0.pos["position"], r1.pos["position"], "units"),
        row("Recommended order quantity", r0.rec.get("qty", 0.0), r1.rec.get("qty", 0.0), "units"),
        row("Stock-out probability", r0.stockout_prob, r1.stockout_prob, "prob"),
        row("Expected shortage over lead time", r0.exp_short, r1.exp_short, "units"),
        row("Service-level impact (short share)", r0.service_impact, r1.service_impact, "share"),
        row("Days of supply", r0.days_supply, r1.days_supply, "days"),
    ]
    return {"sku": inp.sku, "location": inp.loc_code, "change_pct": change_pct, "rows": rows, "risk_before": r0.risk_level, "risk_after": r1.risk_level}
