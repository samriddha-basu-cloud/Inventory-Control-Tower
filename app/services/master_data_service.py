"""Master Data Health Score and UOM tooling.

Health score = mean pass-rate (0–100) over ten checks, each measured over the items it applies to:
duplicate SKU · missing cost · missing lead time · invalid UOM · missing supplier · missing location · invalid MOQ ·
invalid order multiple · missing shelf life (lot-tracked/perishable) · missing service level (no explicit target).
"""
from __future__ import annotations

import re
from collections import defaultdict

from ..models import Item, ItemLocationSource, ItemSupplier, ItemUom
from ..utils import uom as uomlib
from .snapshot import Snapshot

CHECKS = [
    ("duplicate_sku", "Duplicate SKU"), ("missing_cost", "Missing cost"), ("missing_lead_time", "Missing lead time"), ("invalid_uom", "Invalid UOM"),
    ("missing_supplier", "Missing supplier / source"), ("missing_location", "Missing location (stocked nowhere)"), ("invalid_moq", "Invalid MOQ"),
    ("invalid_multiple", "Invalid order multiple"), ("missing_shelf_life", "Missing shelf life"), ("missing_service_level", "Missing service level"),
]


def quality(snap: Snapshot) -> dict:
    items = Item.query.all()
    by_norm = defaultdict(list)
    for it in items:
        by_norm[re.sub(r"[^A-Z0-9]", "", it.sku.upper())].append(it.sku)
    dups = {s for v in by_norm.values() if len(v) > 1 for s in v}
    sup_rows = defaultdict(list)
    for r in ItemSupplier.query.all():
        sup_rows[r.item_id].append(r)
    ils = defaultdict(list)
    for r in ItemLocationSource.query.all():
        ils[r.item_id].append(r)
    stocked = {k[0] for k in snap.inputs}
    issues, passes, applic = [], defaultdict(int), defaultdict(int)

    def rec(check, it, ok, detail, sev="MEDIUM"):
        applic[check] += 1
        if ok:
            passes[check] += 1
        else:
            issues.append({"check": check, "label": dict(CHECKS)[check], "sku": it.sku, "detail": detail, "severity": sev})

    for it in items:
        rec("duplicate_sku", it, it.sku not in dups, "Another SKU normalises to the same code", "HIGH")
        rec("missing_cost", it, bool(it.unit_cost and it.unit_cost > 0), "unit cost is empty or zero", "HIGH")
        has_lt = any(r.lead_time_days for r in sup_rows.get(it.id, [])) or any(r.lead_time_days for r in ils.get(it.id, []))
        rec("missing_lead_time", it, has_lt, "no supplier/source lead time", "HIGH")
        rec("invalid_uom", it, uomlib.is_valid_uom(it.uom), f"UOM '{it.uom}' is not a recognised unit", "HIGH")
        buy = it.item_type not in ("FG",) or bool(sup_rows.get(it.id))
        rec("missing_supplier", it, bool(sup_rows.get(it.id) or ils.get(it.id)), "no supplier or internal source", "MEDIUM")
        rec("missing_location", it, it.id in stocked, "item has no stock, demand or orders at any location", "LOW")
        rec("invalid_moq", it, it.moq is None or (it.moq > 0 and (not it.order_multiple or it.moq >= it.order_multiple)), f"MOQ {it.moq} is not compatible with multiple {it.order_multiple}", "MEDIUM")
        rec("invalid_multiple", it, it.order_multiple is None or it.order_multiple > 0, f"order multiple {it.order_multiple} must be > 0", "MEDIUM")
        if it.lot_tracked or it.shelf_life_days is not None:
            rec("missing_shelf_life", it, bool(it.shelf_life_days), "lot-tracked item without shelf life", "MEDIUM")
        rec("missing_service_level", it, it.target_service_level is not None, "no explicit target service level (policy default applies)", "LOW")
    per_check = []
    for code, label in CHECKS:
        n = applic.get(code, 0)
        rate = (passes[code] / n) if n else None
        per_check.append({"check": code, "label": label, "applicable": n, "passed": passes[code], "rate": rate})
    rates = [c["rate"] for c in per_check if c["rate"] is not None]
    score = 100.0 * sum(rates) / len(rates) if rates else 100.0
    return {"score": score, "checks": per_check, "issues": sorted(issues, key=lambda x: ({"HIGH": 0, "MEDIUM": 1, "LOW": 2}[x["severity"]], x["sku"])), "items": len(items),
            "band": "NORMAL" if score >= 90 else "WATCH" if score >= 80 else "ATTENTION" if score >= 65 else "CRITICAL"}


def uom_table() -> list[dict]:
    out = []
    for r in ItemUom.query.all():
        out.append({"sku": Item.query.get(r.item_id).sku, "from": r.from_uom, "to": r.to_uom, "factor": r.factor})
    return out
