"""Shared helpers for route modules."""
from __future__ import annotations

from datetime import date

from flask import flash, g, redirect, request, session, url_for

from ..models import Item
from ..services.snapshot import PairFilter, Snapshot, get_snapshot


def flt() -> PairFilter:
    return PairFilter.from_mapping(session.get("filters", {}))


def snap() -> Snapshot:
    return get_snapshot()


def has_data() -> bool:
    return Item.query.first() is not None


def actor() -> str:
    return (g.user or {}).get("username") if getattr(g, "user", None) else "anonymous"


def role() -> str:
    return (g.user or {}).get("role") if getattr(g, "user", None) else "Viewer"


def facets(s: Snapshot) -> dict:
    f = getattr(s, "_facets", None)
    if f is not None:
        return f
    inputs = list(s.inputs.values())
    f = {
        "region": sorted({i.loc.get("region") for i in inputs if i.loc.get("region")}),
        "business_unit": sorted({i.item.get("business_unit") for i in inputs if i.item.get("business_unit")}),
        "industry": sorted({i.item.get("industry") for i in inputs if i.item.get("industry")}),
        "family": sorted({i.item.get("family_code") for i in inputs if i.item.get("family_code")}),
        "location": sorted({i.loc_code for i in inputs}),
        "warehouse": sorted({i.loc_code for i in inputs if i.loc.get("loc_type") in ("WAREHOUSE", "CDC", "RDC", "3PL", "MFC", "DARK_STORE")}),
        "supplier": sorted({s.suppliers[i.supplier_id]["code"] for i in inputs if i.supplier_id in s.suppliers}),
        "customer": sorted({c["code"] for c in s.customers.values()}),
        "sku": sorted({i.sku for i in inputs}),
    }
    s._facets = f
    return f


def back(default_endpoint: str = "main.home"):
    nxt = request.form.get("next") or request.args.get("next")
    if nxt and nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(url_for(default_endpoint))


def ok(msg: str):
    flash(msg, "success")


def err(msg: str):
    flash(msg, "error")


def fnum(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def fdate(v):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None
