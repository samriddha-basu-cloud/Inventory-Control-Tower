"""Reconciliation of ICT inventory vs ERP / WMS / 3PL / physical counts, plus data-latency tracking.

Statuses: MATCH, WITHIN_TOLERANCE, VARIANCE, MISSING_IN_ICT, MISSING_IN_SOURCE, DUPLICATE, NEGATIVE, UNIT_MISMATCH,
LOCATION_MISMATCH, TIMING. Tolerances and enabled checks are configuration (`recon.rules`).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from ..models import ExternalBalance, InventoryTransaction, ItemUom, SyncStatus
from ..utils import uom as uomlib
from . import ledger_service as ledger
from . import settings_service as S
from .snapshot import Snapshot

BAD = {"VARIANCE", "MISSING_IN_ICT", "MISSING_IN_SOURCE", "DUPLICATE", "NEGATIVE", "UNIT_MISMATCH", "LOCATION_MISMATCH"}


def reconcile(snap: Snapshot, systems: list[str] | None = None) -> dict:
    rules = S.get("recon.rules")
    checks = set(rules.get("checks", []))
    tol_pct, tol_abs = rules["tolerance_pct"], rules["tolerance_abs"]
    sku_by = {i["sku"]: iid for iid, i in snap.items.items()}
    loc_by = {l["code"]: lid for lid, l in snap.locs.items()}
    factors = defaultdict(dict)
    for u in ItemUom.query.all():
        factors[u.item_id][(u.from_uom, u.to_uom)] = u.factor
    ext = ExternalBalance.query.all()
    if systems:
        ext = [e for e in ext if e.system in systems]
    seen: dict[tuple, list] = defaultdict(list)
    for e in ext:
        seen[(e.system, e.sku_raw, e.location_raw)].append(e)
    covered_locs = defaultdict(set)     # system -> location ids it reports on
    rows = []
    for (system, sku, locc), recs in seen.items():
        iid, lid = sku_by.get(sku), loc_by.get(locc)
        if lid:
            covered_locs[system].add(lid)
        e = recs[-1]
        row = {"system": system, "sku": sku, "location": locc, "source_qty": sum(r.quantity for r in recs) if len(recs) == 1 else e.quantity,
               "uom": e.uom, "ict_qty": None, "variance": None, "variance_pct": None, "last_sync": e.last_sync, "status": "MATCH", "note": "",
               "flags": [], "item_id": iid, "location_id": lid}
        if len(recs) > 1 and "duplicate" in checks:
            row["flags"].append("DUPLICATE")
            row["note"] = f"{len(recs)} records for the same key; last value used ({', '.join(f'{r.quantity:,.0f}' for r in recs)})"
        if iid is None and "missing" in checks:
            row.update(status="MISSING_IN_ICT", note="SKU not found in ICT master data")
            rows.append(_fin(row))
            continue
        if lid is None and "location" in checks:
            row.update(status="LOCATION_MISMATCH", note="Location code is not mapped to an ICT node")
            rows.append(_fin(row))
            continue
        if iid is None or lid is None:
            continue
        item = snap.items[iid]
        qty = e.quantity
        if uomlib.norm(e.uom) != uomlib.norm(item["uom"]) and "unit" in checks:
            try:
                qty = uomlib.convert(e.quantity, e.uom, item["uom"], factors.get(iid))
                row["flags"].append("UNIT_CONVERTED")
                row["note"] += f" unit {e.uom}→{item['uom']} converted;"
            except uomlib.UomConversionError as ex:
                row.update(status="UNIT_MISMATCH", note=f"{ex}")
                row["ict_qty"] = snap.results[(iid, lid)].pos["on_hand"] if (iid, lid) in snap.results else 0.0
                rows.append(_fin(row))
                continue
        row["source_qty"] = qty
        ict = snap.results[(iid, lid)].pos["on_hand"] if (iid, lid) in snap.results else 0.0
        row["ict_qty"] = ict
        var = qty - ict
        row["variance"], row["variance_pct"] = var, (var / ict) if ict else (1.0 if var else 0.0)
        if qty < 0 and "negative" in checks:
            row.update(status="NEGATIVE", note="Source reports negative inventory")
        elif abs(var) < 1e-9:
            row["status"] = "MATCH"
        elif abs(var) <= tol_abs or abs(var) <= tol_pct * max(abs(ict), 1.0):
            row["status"] = "WITHIN_TOLERANCE"
        else:
            row["status"] = "VARIANCE"
            if "timing" in checks and e.last_sync:
                net = _net_since(iid, lid, e.last_sync)
                if abs((ict - net) - qty) <= max(tol_abs, tol_pct * max(abs(ict), 1.0)):
                    row.update(status="TIMING", note=f"Explained by {net:+,.0f} units of ICT movements after the source snapshot ({e.last_sync:%d %b %H:%M})")
        if "DUPLICATE" in row["flags"]:
            row["status"] = "DUPLICATE" if row["status"] in ("MATCH", "WITHIN_TOLERANCE") else row["status"]
        rows.append(_fin(row))
    # missing in source: ICT stock where the system reports the location but not this SKU
    if "missing" in checks:
        have = {(r["system"], r["item_id"], r["location_id"]) for r in rows if r["item_id"]}
        for system in ({e.system for e in ext} - {"PHYSICAL"}):
            for (iid, lid), r in snap.results.items():
                if lid in covered_locs[system] and r.pos["on_hand"] > 0 and (system, iid, lid) not in have:
                    rows.append(_fin({"system": system, "sku": snap.items[iid]["sku"], "location": snap.locs[lid]["code"], "source_qty": 0.0, "uom": "EA",
                                      "ict_qty": r.pos["on_hand"], "variance": -r.pos["on_hand"], "variance_pct": -1.0, "last_sync": None,
                                      "status": "MISSING_IN_SOURCE", "note": f"{system} reports this location but has no record of the SKU",
                                      "flags": [], "item_id": iid, "location_id": lid}))
    summary = defaultdict(int)
    for r in rows:
        summary[r["status"]] += 1
    n = len(rows) or 1
    by_system = defaultdict(lambda: {"records": 0, "bad": 0, "abs_var_units": 0.0})
    for r in rows:
        s = by_system[r["system"]]
        s["records"] += 1
        s["bad"] += 1 if r["status"] in BAD else 0
        s["abs_var_units"] += abs(r["variance"] or 0.0)
    return {"rows": rows, "summary": dict(summary), "match_rate": (summary["MATCH"] + summary["WITHIN_TOLERANCE"]) / n,
            "by_system": dict(by_system), "rules": rules}


def _fin(row):
    row["variance_pct_abs"] = abs(row["variance_pct"]) if row.get("variance_pct") is not None else 0.0
    return row


def _net_since(item_id, loc_id, since: datetime) -> float:
    """Net on-hand change at a location from ledger transactions after `since`."""
    states = {s["code"] for s in S.state_defs() if s["counts_on_hand"]}
    net = 0.0
    q = InventoryTransaction.query.filter(InventoryTransaction.item_id == item_id, InventoryTransaction.occurred_at > since,
                                          (InventoryTransaction.location_id == loc_id) | (InventoryTransaction.to_location_id == loc_id))
    for t in q.all():
        net += ledger._delta_at(t, loc_id, states)
    return net


def latency_report(snap: Snapshot) -> list[dict]:
    """Age of each source feed vs expected frequency; plus last ledger update."""
    now = S.now()
    out = []
    for s in SyncStatus.query.order_by(SyncStatus.source).all():
        age = ((now - s.last_sync).total_seconds() / 3600.0) if s.last_sync else None
        ratio = (age / s.expected_every_hours) if (age is not None and s.expected_every_hours) else None
        out.append({"source": s.source, "last_sync": s.last_sync, "age_hours": age, "expected_hours": s.expected_every_hours,
                    "ratio": ratio, "records": s.records, "note": s.note,
                    "status": "NEVER" if age is None else ("STALE" if ratio and ratio > 1 else "FRESH")})
    last_txn = InventoryTransaction.query.order_by(InventoryTransaction.occurred_at.desc()).first()
    out.append({"source": "LEDGER (last inventory update)", "last_sync": last_txn.occurred_at if last_txn else None,
                "age_hours": ((now - last_txn.occurred_at).total_seconds() / 3600.0) if last_txn else None, "expected_hours": None, "ratio": None,
                "records": InventoryTransaction.query.count(), "note": "most recent posted transaction", "status": "INFO"})
    return out
