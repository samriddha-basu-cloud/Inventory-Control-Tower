"""Root-cause clustering: many symptom alerts → one incident.

Alerts are NOT grouped by text similarity. Two alerts are linked only when they share a *causal dimension* value:
shipment, purchase order, port, transport lane, supplier (when delayed), production order - and their time-to-impact
lies within a window (default 14 days). Linked alerts form connected components (union-find); a component with two or
more alerts becomes an Incident carrying the shared dimensions, aggregated impact and an 8-question explanation.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from ..extensions import db
from ..models import Alert, Incident
from . import settings_service as S
from .snapshot import Snapshot

CAUSAL_DIMS = ["shipment", "po", "port", "lane", "supplier", "prod_order"]
DIM_PRIORITY = ["port", "shipment", "po", "supplier", "lane", "prod_order"]
WINDOW_DAYS = 14
ACTIVE = ["New", "Acknowledged", "Investigating", "Action Proposed", "Approved"]


class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _tokens(a: Alert) -> list[tuple[str, str]]:
    d = a.dims or {}
    out = []
    for dim in CAUSAL_DIMS:
        v = d.get(dim)
        if not v:
            continue
        for x in (v if isinstance(v, list) else [v]):
            if x:
                out.append((dim, str(x)))
    return out


def cluster(snap: Snapshot) -> int:
    alerts = [a for a in Alert.query.filter(Alert.status.in_(ACTIVE + ["Executed"]), Alert.suppressed.is_(False)).all()]
    # detach from previous incident linkage; incidents are rebuilt but keep status/owner by cluster_key
    prev = {i.cluster_key: i for i in Incident.query.all()}
    for a in alerts:
        a.incident_id = None
    n = len(alerts)
    uf = _UF(n)
    by_token: dict[tuple, list[int]] = defaultdict(list)
    for i, a in enumerate(alerts):
        for t in _tokens(a):
            by_token[t].append(i)
    for tok, idxs in by_token.items():
        for j in idxs[1:]:
            a, b = alerts[idxs[0]], alerts[j]
            ta, tb = a.time_to_impact_days, b.time_to_impact_days
            if ta is None or tb is None or abs(ta - tb) <= WINDOW_DAYS:
                uf.union(idxs[0], j)
    comps: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        comps[uf.find(i)].append(i)
    kept_keys = set()
    count = 0
    for members in comps.values():
        if len(members) < 2:
            continue
        group = [alerts[i] for i in members]
        # shared dimensions: value -> how many alerts carry it
        cnt: dict[tuple, int] = Counter()
        for a in group:
            for t in set(_tokens(a)):
                cnt[t] += 1
        primary = None
        for dim in DIM_PRIORITY:
            cands = [(c, t) for t, c in cnt.items() if t[0] == dim and c >= 2]
            if cands:
                primary = max(cands)[1]
                break
        if not primary:
            continue
        key = f"{primary[0]}:{primary[1]}"
        kept_keys.add(key)
        inc = prev.get(key) or Incident(cluster_key=key, status="New")
        if inc.id is None:
            db.session.add(inc)
        _fill(inc, group, primary, snap, cnt)
        db.session.flush()
        if not inc.incident_no:
            inc.incident_no = f"INC-{inc.id:05d}"
        for a in group:
            a.incident_id = inc.id
        count += 1
    for key, inc in prev.items():
        if key not in kept_keys and inc.status not in ("Resolved", "Dismissed"):
            inc.status = "Resolved"
    db.session.flush()
    return count


def _fill(inc: Incident, group: list[Alert], primary: tuple, snap: Snapshot, cnt) -> None:
    skus = sorted({(a.dims or {}).get("sku") for a in group if (a.dims or {}).get("sku")})
    nodes = sorted({(a.dims or {}).get("node") for a in group if (a.dims or {}).get("node")})
    pos = sorted({p for a in group for p in (a.dims or {}).get("po", [])})
    ships = sorted({s for a in group for s in (a.dims or {}).get("shipment", [])})
    sups = sorted({s for a in group for s in (a.dims or {}).get("supplier", [])})
    ports = sorted({p for a in group for p in (a.dims or {}).get("port", [])})
    lanes = sorted({p for a in group for p in (a.dims or {}).get("lane", [])})
    reasons = Counter(r for a in group for r in (a.dims or {}).get("reason", []))
    rev = sum((a.impact or {}).get("revenue_at_risk", 0.0) for a in group if a.alert_type not in ("PO_DELAY", "ETA_CHANGE", "SUPPLIER_DELAY"))
    prod = sum((a.impact or {}).get("production_at_risk", 0.0) for a in group if a.alert_type not in ("PO_DELAY", "ETA_CHANGE", "SUPPLIER_DELAY"))
    inv = sum((a.impact or {}).get("inventory_impact", 0.0) or (a.impact or {}).get("value_at_risk", 0.0) for a in group if a.alert_type in ("PO_DELAY", "ETA_CHANGE"))
    svc = [(a.impact or {}).get("service_impact", 0.0) for a in group if (a.impact or {}).get("service_impact")]
    customers = max([(a.impact or {}).get("customers", 0) for a in group] + [0])
    sev_order = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    sev = max((a.severity for a in group), key=sev_order.index)
    dim, val = primary
    if dim == "port":
        title = f"Port delay at {val}: {len(ships)} shipment(s), {len(skus)} SKU(s) affected"
        cause_type = "PORT_DELAY"
    elif dim == "shipment":
        title = f"Shipment {val} delayed: {len(skus)} SKU(s) affected"
        cause_type = "TRANSPORT_DELAY"
    elif dim == "po":
        title = f"Purchase order {val} delayed: {len(skus)} SKU(s) affected"
        cause_type = "SUPPLIER_DELAY"
    elif dim == "supplier":
        title = f"Supplier {val} delays affecting {len(skus)} SKU(s) across {len(nodes)} node(s)"
        cause_type = "SUPPLIER_DELAY"
    elif dim == "lane":
        title = f"Lane {val} disrupted: {len(skus)} SKU(s) affected"
        cause_type = "TRANSPORT_DELAY"
    else:
        title = f"Production order {val} at risk: {len(skus)} component SKU(s)"
        cause_type = "PRODUCTION_SHORTAGE"
    root = reasons.most_common(1)[0][0] if reasons else title
    top = sorted(group, key=lambda a: -a.priority_score)
    breadth = min(15.0, 3.0 * len(skus))
    inc.title, inc.root_cause, inc.cause_type, inc.severity = title, root, cause_type, sev
    inc.dims = {"primary": {dim: val}, "ports": ports, "lanes": lanes, "suppliers": sups, "shipments": ships, "pos": pos}
    inc.impact = {"affected_skus": skus, "affected_nodes": nodes, "affected_pos": pos, "shipments": ships, "revenue_at_risk": rev, "production_at_risk": prod,
                  "inventory_impact": inv, "service_impact": (sum(svc) / len(svc)) if svc else 0.0, "customers": customers, "alerts": len(group),
                  "critical_alerts": sum(1 for a in group if a.severity == "CRITICAL")}
    inc.priority_score = min(100.0, sum(a.priority_score for a in top[:3]) / min(3, len(top)) + breadth)
    inc.owner = inc.owner or ("Supply Chain Manager" if sev in ("HIGH", "CRITICAL") else "Inventory Planner")
    inc.explain = {
        "what": title,
        "why": f"{root}. {len(group)} alerts share {dim} '{val}'" + (f" and {len(pos)} PO(s)" if pos else "") + ".",
        "when": f"Earliest impact in ~{min([a.time_to_impact_days for a in group if a.time_to_impact_days is not None] or [0]):.0f} days",
        "where": f"Nodes: {', '.join(nodes[:8])}" + (f"; port {', '.join(ports)}" if ports else "") + (f"; lane {lanes[0]}" if lanes else ""),
        "who": f"{len(skus)} SKU(s), {len(sups)} supplier(s), {customers} customer(s)",
        "how_big": {"revenue_at_risk": rev, "production_at_risk": prod, "inventory_impact": inv, "service_impact": inc.impact["service_impact"]},
        "options": "Expedite or re-route delayed shipments, transfer stock from nodes with cover, allocate scarce stock by policy, raise safety stock for affected lane.",
        "if_nothing": f"Expected lost sales/production impact of about {rev + prod:,.0f} and service-level erosion as projected shortfalls materialise.",
    }
