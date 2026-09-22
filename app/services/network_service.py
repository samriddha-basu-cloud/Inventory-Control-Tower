"""Multi-echelon network model for the graph and node tables: Supplier → Plant → DC → Store/Customer (arbitrary depth)."""
from __future__ import annotations

from collections import defaultdict

from flask import url_for

from ..models import Shipment, TransferOrder
from ..rules.thresholds import worse
from .snapshot import PairFilter, Snapshot


def build(snap: Snapshot, f: PairFilter | None = None, sku: str | None = None) -> dict:
    keys = snap.keys(f)
    if sku:
        keys = [k for k in keys if snap.inputs[k].sku == sku]
    per_loc = defaultdict(lambda: {"on_hand": 0.0, "available": 0.0, "inbound": 0.0, "outbound": 0.0, "ss": 0.0, "demand": 0.0, "value": 0.0, "risk": "LOW", "pairs": 0,
                                   "at_risk": 0, "excess_value": 0.0})
    for k in keys:
        inp, r = snap.inputs[k], snap.results[k]
        n = per_loc[k[1]]
        n["on_hand"] += r.pos["on_hand"]
        n["available"] += r.pos["available"]
        n["inbound"] += r.pos["in_transit"] + r.pos["on_order"]
        n["ss"] += r.ss
        n["demand"] += r.d_mean
        n["value"] += r.on_hand_value
        n["excess_value"] += r.excess_value
        n["pairs"] += 1
        n["outbound"] += sum(t["qty"] for t in inp.transfers_out)
        if r.d_mean > 0 or r.pos["on_hand"] > 0:
            n["risk"] = worse(n["risk"], r.risk_level)
        n["at_risk"] += 1 if r.risk_level in ("HIGH", "CRITICAL") else 0
    supplier_ids = {snap.inputs[k].supplier_id for k in keys if snap.inputs[k].supplier_id}
    # positions: column = echelon (suppliers = 0), rows spread evenly
    cols: dict[int, list] = defaultdict(list)
    for sid in sorted(supplier_ids):
        cols[0].append(("S", sid))
    for lid in per_loc:
        cols[snap.locs[lid]["echelon"] or 1].append(("L", lid))
    nodes, pos = [], {}
    maxv = max([v["value"] for v in per_loc.values()] + [1.0])
    for col, members in sorted(cols.items()):
        members.sort(key=lambda m: ((snap.locs[m[1]]["industry"] or "", snap.locs[m[1]]["region"] or "", snap.locs[m[1]]["code"]) if m[0] == "L" else (snap.suppliers[m[1]].get("industry") or snap.suppliers[m[1]]["code"].split("-")[0], "", snap.suppliers[m[1]]["code"])))
        for i, (typ, ident) in enumerate(members):
            y = (i + 0.5) / len(members)
            if typ == "S":
                s = snap.suppliers[ident]
                nid = f"S{ident}"
                nodes.append({"id": nid, "label": s["code"], "x": col, "y": y, "size": 16, "risk": "LOW", "kind": "Supplier",
                              "hover": f"<b>{s['name']}</b><br>Supplier · {s['country']} · tier {s['tier']}", "url": url_for("inventory.supplier360", code=s["code"])})
            else:
                l = snap.locs[ident]
                v = per_loc[ident]
                nid = f"L{ident}"
                cap = l.get("capacity_units") or 0
                util = (v["on_hand"] / cap) if cap else None
                nodes.append({"id": nid, "label": l["code"], "x": col, "y": y, "size": 14 + 30 * (v["value"] / maxv) ** 0.5, "risk": v["risk"], "kind": l["loc_type"],
                              "hover": (f"<b>{l['name']}</b> ({l['loc_type']})<br>On hand {v['on_hand']:,.0f} · Available {v['available']:,.0f}<br>Inbound {v['inbound']:,.0f} · Outbound {v['outbound']:,.0f}"
                                        f"<br>Safety stock {v['ss']:,.0f} · Demand/day {v['demand']:,.0f}<br>Risk {v['risk']} · Capacity util {util:.0%}" if util is not None else
                                        f"<b>{l['name']}</b> ({l['loc_type']})<br>On hand {v['on_hand']:,.0f} · Available {v['available']:,.0f}<br>Inbound {v['inbound']:,.0f}<br>Risk {v['risk']}"),
                              "url": url_for("inventory.location360", code=l["code"])})
            pos[nid] = (col, y)
    edges = []
    seen = set()
    for lid in per_loc:
        parent = snap.locs[lid].get("parent_id")
        if parent in per_loc and (parent, lid) not in seen:
            seen.add((parent, lid))
            edges.append({"src": f"L{parent}", "dst": f"L{lid}", "kind": "supply"})
    for k in keys:
        inp = snap.inputs[k]
        if inp.supplier_id and not inp.source_loc_id and (f"S{inp.supplier_id}", f"L{k[1]}") not in seen:
            seen.add((f"S{inp.supplier_id}", f"L{k[1]}"))
            edges.append({"src": f"S{inp.supplier_id}", "dst": f"L{k[1]}", "kind": "supply"})
    for s in Shipment.query.filter(Shipment.status != "DELIVERED").all():
        if s.supplier_id and s.dest_location_id and f"L{s.dest_location_id}" in pos and f"S{s.supplier_id}" in pos:
            edges.append({"src": f"S{s.supplier_id}", "dst": f"L{s.dest_location_id}", "kind": "shipment"})
    for t in TransferOrder.query.filter(TransferOrder.status.in_(["IN_TRANSIT", "OPEN"])).all():
        if f"L{t.from_location_id}" in pos and f"L{t.to_location_id}" in pos:
            edges.append({"src": f"L{t.from_location_id}", "dst": f"L{t.to_location_id}", "kind": "transfer"})
    rows = []
    for lid, v in per_loc.items():
        l = snap.locs[lid]
        cap = l.get("capacity_units") or 0
        rows.append({"code": l["code"], "name": l["name"], "type": l["loc_type"], "region": l["region"], "echelon": l["echelon"], "on_hand": v["on_hand"], "available": v["available"],
                     "inbound": v["inbound"], "outbound": v["outbound"], "ss": v["ss"], "demand": v["demand"], "risk": v["risk"], "at_risk": v["at_risk"], "value": v["value"],
                     "utilization": (v["on_hand"] / cap) if cap else None})
    return {"nodes": nodes, "edges": edges, "rows": sorted(rows, key=lambda r: (r["echelon"], r["code"]))}
