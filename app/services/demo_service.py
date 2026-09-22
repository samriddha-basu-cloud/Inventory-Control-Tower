"""Demo network loader ("LOAD DEMO NETWORK").

Builds internally-consistent synthetic data for up to seven industries, including planted "stories" that make
every module worth exploring: a congested port hitting many SKUs (root-cause incident), an ECO cut-over,
EOL components, near-expiry / quarantined lots, excess-vs-shortage pairs for rebalancing, reconciliation
variances, a stale WMS feed, a demand spike, obsolete stock and a capacity breach.
"""
from __future__ import annotations

import math
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta

import numpy as np

from ..extensions import db
from ..models import (Allocation, Alert, Approval, Action, AuditLog, BomLine, CalendarEvent, Carrier, ControlPolicy, Customer,
                      Demand, Event, Execution, Experiment, ExternalBalance, Forecast, Incident, InventoryBalance,
                      InventoryTransaction, Item, ItemLocationSource, ItemSupplier, ItemUom, Job, KpiSnapshot,
                      LeadTimeObservation, Location, Lot, Notification, Peg, ProductFamily, ProductionOrder, Promotion,
                      PurchaseOrder, PurchaseOrderLine, Recommendation, ReplenishmentPolicy, ReturnRecord, ReusableAsset,
                      Risk, SafetyStockPolicy, SalesOrder, SalesOrderLine, Scenario, SerialNumber, Shipment, ShipmentLine,
                      Supplier, SyncStatus, TransferOrder)
from ..utils.geo import route_km
from . import ledger_service as ledger
from . import settings_service as S
from .demo_specs import INDUSTRY_LABELS, SPECS

OPERATIONAL_TABLES = [Peg, Allocation, Approval, Execution, Action, Recommendation, Alert, Incident, Risk, Scenario, Experiment,
                      Event, Notification, ReturnRecord, ReusableAsset, ExternalBalance, KpiSnapshot, SerialNumber,
                      InventoryTransaction, InventoryBalance, ShipmentLine, Shipment, TransferOrder, ProductionOrder,
                      SalesOrderLine, SalesOrder, PurchaseOrderLine, PurchaseOrder, LeadTimeObservation, Forecast, Demand,
                      Promotion, CalendarEvent, BomLine, ItemLocationSource, ItemSupplier, ItemUom, Lot, Item, ProductFamily,
                      Customer, Supplier, Carrier, ReplenishmentPolicy, SafetyStockPolicy]


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def reset_operational_data() -> None:
    """Delete operational data (keeps configuration: settings, rules, KPIs, roles, users, profiles, autonomy, states)."""
    for m in OPERATIONAL_TABLES:
        db.session.query(m).delete()
    ControlPolicy.query.filter(ControlPolicy.scope_level.in_(["SKU", "SKU_LOCATION", "NODE"])).delete(synchronize_session=False)
    AuditLog.query.filter(AuditLog.category.in_(["DATA", "CALC", "ACTION", "APPROVAL", "EXEC", "SCENARIO"])).delete(synchronize_session=False)
    db.session.commit()


# ------------------------------------------------------------------------------------------------ series
def _seasonal(woy, amp=0.35, peak=27):
    return 1 + amp * math.sin(2 * math.pi * (woy - peak + 13) / 52)


def gen_series(pat: str, base: float, week_dates: list[date], rng, n_hist: int = 52) -> tuple[list[float], list[float]]:
    """Returns (actual[0:n_hist], expected[0:len(week_dates)])."""
    n = len(week_dates)
    exp = np.full(n, float(base))
    woy = np.array([d.isocalendar()[1] for d in week_dates])
    t = np.arange(n)
    if pat in ("seasonal",):
        exp = base * np.array([_seasonal(w, 0.35, 27) for w in woy])
    elif pat == "seasonal_winter":
        exp = base * (1 + 0.65 * np.cos(2 * np.pi * (woy - 1) / 52))
    elif pat == "trend_up":
        exp = base * (0.8 + 0.5 * t / (n - 1))
    elif pat == "declining":
        exp = base * np.maximum(0.12, 1.25 - 1.15 * t / (n - 1))
    elif pat == "dead":
        exp = np.zeros(n)
    elif pat == "spike":
        exp = np.full(n, float(base))
    exp = np.maximum(exp, 0)
    h = exp[:n_hist].copy()
    if pat == "erratic":
        act = h * rng.lognormal(-0.08, 0.42, n_hist)
    elif pat == "intermittent":
        p = 0.27
        size = rng.gamma(2.0, max(base, 0.01) / (p * 2.0), n_hist)
        act = np.where(rng.random(n_hist) < p, size, 0.0)
    elif pat == "lumpy":
        p = 0.4
        size = rng.lognormal(math.log(max(base, 0.01) / p) - 0.4, 0.9, n_hist)
        act = np.where(rng.random(n_hist) < p, size, 0.0)
    elif pat == "dead":
        act = np.zeros(n_hist)
    elif pat == "spike":
        act = h * rng.normal(1.0, 0.10, n_hist)
        act[-4:] *= np.array([1.5, 2.0, 2.4, 2.6])
        exp[n_hist:] = base * 1.4
    else:
        act = h * rng.normal(1.0, 0.11, n_hist)
    act = np.maximum(act, 0)
    if base < 30 and pat != "dead":
        act = np.where(act > 0, np.maximum(1, np.round(act)), 0)
    else:
        act = np.round(act)
    return act.tolist(), exp.tolist()


# ------------------------------------------------------------------------------------------------ main loader
class Ctx:
    def __init__(self, seed: int):
        self.rng = np.random.default_rng(seed)
        self.today = S.today()
        self.cur_w = week_start(self.today)
        self.hist_weeks = [self.cur_w - timedelta(weeks=52 - i) for i in range(52)]
        self.future_weeks = [self.cur_w + timedelta(weeks=i) for i in range(26)]
        self.all_weeks = self.hist_weeks + self.future_weeks
        self.txns: list[dict] = []
        self.seq = 0
        self.carriers: dict[str, Carrier] = {}
        self.pair_stats: dict[tuple, dict] = {}
        self.summary: dict = defaultdict(int)
        self._lot = 0

    def next_lot(self) -> int:
        self._lot += 1
        return self._lot

    def txn(self, **kw):
        self.seq += 1
        kw["_seq"] = self.seq
        self.txns.append(kw)


def load_demo(industries: list[str] | None = None, seed: int = 42, reset: bool = True, run_detection: bool = True) -> dict:
    industries = [i for i in (industries or list(SPECS)) if i in SPECS] or list(SPECS)
    if reset:
        reset_operational_data()
    ctx = Ctx(seed)
    ctx.carriers = {m: Carrier(code=f"CAR-{m}", name=n, mode=m, source_system="DEMO") for m, n in
                    [("ROAD", "Blue Line Road Freight"), ("RAIL", "Indian Rail Cargo"), ("SEA", "Oceanic Container Lines"), ("AIR", "SkyBridge Air Cargo")]}
    db.session.add_all(ctx.carriers.values())
    db.session.flush()
    for code in industries:
        _build_industry(ctx, SPECS[code])
    _finalize_ledger(ctx)
    db.session.flush()
    _capacity_tuning(ctx)
    _recon_data(ctx)
    _assets_returns(ctx)
    _sync_status(ctx)
    S.set_value("system.demo_loaded", {"industries": industries, "seed": seed, "at": S.now().isoformat()}, audit=False)
    profile = SPECS[industries[0]]["profile"] if len(industries) == 1 else "GENERAL"
    S.set_value("industry.active", profile, audit=False)
    S.bump_version()
    db.session.commit()
    from . import snapshot
    snapshot.clear_cache()
    out = dict(ctx.summary)
    out["industries"] = industries
    # derived layers (need the snapshot)
    from . import allocation_service, alert_service, kpi_service
    out["allocations"] = allocation_service.run_pegging_all()
    db.session.commit()
    _kpi_history(ctx)
    if run_detection:
        out["detection"] = alert_service.run_detection(actor="demo-loader")
    db.session.commit()
    return out


# ------------------------------------------------------------------------------------------------ industry builder
def _pick_nodes(spec: dict, item: dict, locs: dict) -> list[dict]:
    """Which nodes stock an item. Raw/packaging only upstream; FG below the plant; some MAKE items plant-only."""
    L = list(locs.values())
    if item.get("nofg"):
        return [l for l in L if l["type"] in ("PLANT",) or (l["type"] in ("CDC", "WAREHOUSE") and l["echelon"] == 1)]
    if item["src"] == "MAKE" and spec["code"] in ("AUTOMOTIVE", "ELECTRONICS"):
        return [l for l in L if l["type"] == "PLANT"]
    if item.get("wip"):
        return [l for l in L if l["type"] == "PLANT"]
    if spec["code"] == "MANUFACTURING" and item["src"] == "MAKE":
        return [l for l in L if l["code"] != "MFG-WH-RM"]
    if spec["code"] == "ELECTRONICS":
        return [l for l in L if l["type"] in ("PLANT", "CDC")]
    return L


def _build_industry(ctx: Ctx, spec: dict) -> None:
    rng, today = ctx.rng, ctx.today
    code = spec["code"]
    # suppliers
    sup: list[Supplier] = []
    for (c, n, tier, country, city, lat, lon, geo, rel, ltcv, cap) in spec["suppliers"]:
        s = Supplier(code=c, name=n, tier=tier, country=country, city=city, lat=lat, lon=lon, geo_risk=geo, industry=code,
                     capacity_units_week=cap, region="Import" if country != "India" else "Domestic", source_system="DEMO",
                     quality_rejection_rate=round(0.004 + (1 - rel) * 0.06, 4), payment_terms_days=int(rng.choice([30, 45, 60, 75])))
        s._rel, s._ltcv = rel, ltcv
        sup.append(s)
    db.session.add_all(sup)
    # locations
    locs: dict[str, dict] = {}
    rows = []
    for (c, n, typ, ech, parent, region, city, lat, lon, cap, w, extra) in spec["locations"]:
        l = Location(code=c, name=n, loc_type=typ, echelon=ech, region=region, country="India", city=city, lat=lat, lon=lon,
                     industry=code, business_unit=spec["bu"], capacity_units=cap, capacity_m3=cap * 0.004,
                     is_3pl=bool(extra.get("is3pl")), temp_capable=extra.get("temp", "AMBIENT"), channel=extra.get("channel"),
                     source_system="DEMO")
        rows.append((l, parent, w, extra))
    db.session.add_all([r[0] for r in rows])
    db.session.flush()
    byc = {r[0].code: r[0] for r in rows}
    for l, parent, w, extra in rows:
        if parent:
            l.parent_id = byc[parent].id
        locs[l.code] = {"obj": l, "code": l.code, "type": l.loc_type, "echelon": l.echelon, "parent": parent, "w": w, "extra": extra}
    custs = []
    for (c, n, seg, prio, contract, region) in spec["customers"]:
        custs.append(Customer(code=c, name=n, segment=seg, priority=prio, contract_priority=contract, region=region, industry=code,
                              country="India", source_system="DEMO"))
    db.session.add_all(custs)
    # items
    items: dict[str, dict] = {}
    for it in spec["items"]:
        o = Item(sku=it["sku"], description=it["desc"], family_code=it["fam"], category=it["cat"], industry=code, business_unit=spec["bu"],
                 item_type=it.get("itype", "FG" if it["src"] == "MAKE" else "COMPONENT"), uom="EA", unit_cost=it["cost"],
                 selling_price=round(it["cost"] * (1 + it["margin"]) if it["margin"] else 0, 2), moq=it["moq"], order_multiple=it["mult"],
                 shelf_life_days=it.get("shelf"), criticality=it["crit"], lot_tracked=bool(it.get("lot")), serial_tracked=bool(it.get("serial")),
                 lifecycle_status=it.get("life", "ACTIVE"), weight_kg=it["weight"], volume_m3=max(it["weight"] * 0.002, 0.00001),
                 temp_requirement=it.get("temp", "AMBIENT"), hazardous=bool(it.get("hazmat")), country_of_origin="India",
                 revision=it.get("rev"), superseded_by_sku=it.get("superseded"), markdown_pct=it.get("markdown", 0.0),
                 line_stop_cost_per_unit=it.get("lstop"), substitute_group=it.get("alt"), source_system="DEMO",
                 ved={"Critical": "V", "High": "E", "Medium": "E", "Low": "D"}[it["crit"]],
                 sde={"Critical": "S", "High": "D", "Medium": "E", "Low": "E"}[it["crit"]])
        if it.get("eco_days"):
            o.eco_effective_date = today + timedelta(days=it["eco_days"])
            o.eco_reworkable = False
        if it.get("eol_days"):
            o.eol_date = today + timedelta(days=it["eol_days"])
        if it.get("life") == "EOL" and not o.eol_date:
            o.eol_date = today + timedelta(days=200)
        items[it["sku"]] = {"spec": it, "obj": o}
    db.session.add_all([v["obj"] for v in items.values()])
    db.session.flush()
    have = {f.code for f in ProductFamily.query.all()}
    fams = {}
    for v in items.values():
        fams.setdefault(v["obj"].family_code, v["obj"].category)
    db.session.add_all([ProductFamily(code=f, name=f.title(), category=c, source_system="DEMO") for f, c in fams.items() if f not in have])
    for sk, v in items.items():
        it, o = v["spec"], v["obj"]
        if it["src"] == "BUY":
            s = sup[it["sup"]]
            db.session.add(ItemSupplier(item_id=o.id, supplier_id=s.id, is_primary=True, lead_time_days=it["lt"], moq=it["moq"], order_multiple=it["mult"],
                                        price=it["cost"], capacity_units_week=spec["suppliers"][it["sup"]][10],
                                        mode=it.get("mode", "ROAD"), lane=it.get("lane"), origin_port=it.get("port"), source_system="DEMO"))
        if it.get("pack"):
            db.session.add(ItemUom(item_id=o.id, from_uom="CASE", to_uom="EA", factor=it["pack"]))
    for (parent, comp, q) in spec["bom"]:
        db.session.add(BomLine(parent_item_id=items[parent]["obj"].id, component_item_id=items[comp]["obj"].id, qty_per=q,
                               revision=items[parent]["obj"].revision, source_system="DEMO"))
    # ECO: ensure a BOM alternate row for brake actuator revision C
    if code == "AUTOMOTIVE":
        db.session.add(BomLine(parent_item_id=items["AUT-FG-TRN"]["obj"].id, component_item_id=items["AUT-BRK-ACT-C"]["obj"].id, qty_per=1,
                               revision="C", valid_from=today + timedelta(days=21), source_system="DEMO"))
        for b in BomLine.query.filter_by(parent_item_id=items["AUT-FG-TRN"]["obj"].id, component_item_id=items["AUT-BRK-ACT-B"]["obj"].id):
            b.valid_to = today + timedelta(days=21)
    db.session.flush()

    # policies
    for node, params in spec["repl"].items():
        db.session.add(ReplenishmentPolicy(scope_level="NODE", scope_key=node, params=params, notes=f"{code} node policy", source_system="DEMO"))
    for skuk, params in spec["sku_repl"].items():
        db.session.add(ReplenishmentPolicy(scope_level="SKU", scope_key=skuk, params=params, notes="SKU-specific (JIT/JIS)", source_system="DEMO"))
    ss_over = {"AUT-ECU-CTL": {"service_level": 0.995}, "PHM-INJ-INSUL": {"service_level": 0.995, "days_cover": None},
               "SPR-TRB-BLADE": {"service_level": 0.97}, "ELE-IC-MCU32": {"service_level": 0.98}}
    for skuk, params in ss_over.items():
        if skuk in items:
            db.session.add(SafetyStockPolicy(scope_level="SKU", scope_key=skuk, params={k: v for k, v in params.items() if v is not None},
                                             notes="Critical part: higher service level", source_system="DEMO"))
    if code == "RETAIL":
        db.session.add(SafetyStockPolicy(scope_level="SKU_LOCATION", scope_key="RET-ACC-EARB|RET-DC-S", params={"days_cover": 14},
                                         notes="Promo hero SKU: fixed 14-day cover", source_system="DEMO"))
    db.session.flush()

    # per-item generation --------------------------------------------------------------------------------------
    demand_rows, fc_rows = [], []
    node_series: dict[tuple, tuple[list, list]] = {}
    for sk, v in items.items():
        it, o = v["spec"], v["obj"]
        nodes = _pick_nodes(spec, it, locs)
        codes = {n["code"] for n in nodes}
        children = defaultdict(list)
        for n in nodes:
            if n["parent"] in codes:
                children[n["parent"]].append(n["code"])
        leaves_w = {n["code"]: n["w"] for n in nodes if n["w"] > 0}
        if not leaves_w:  # raw/upstream-only items: demand lives at their own top nodes
            tops = [n for n in nodes if n["parent"] not in codes or n["parent"] is None]
            for n in tops:
                leaves_w[n["code"]] = 1.0 / len(tops)
            # nodes with children still get sums of children below
        tw = sum(leaves_w.values()) or 1
        series: dict[str, tuple] = {}
        for c, w in leaves_w.items():
            base = it["weekly"] * w / tw
            phase = rng.integers(0, 6)
            series[c] = gen_series(it["pat"], base, ctx.all_weeks, rng)
        # aggregate upwards (post-order)
        def total(code_):
            own = series.get(code_)
            acts = np.array(own[0]) if own else np.zeros(52)
            exps = np.array(own[1]) if own else np.zeros(78)
            for ch in children.get(code_, []):
                a, e = total(ch)
                acts, exps = acts + a, exps + e
            return acts, exps
        for n in nodes:
            a, e = total(n["code"])
            node_series[(sk, n["code"])] = (a.tolist(), e.tolist())
        # ECO shaping of expected future
        if it.get("eco_days") and it.get("superseded"):
            cut = it["eco_days"] // 7
            for c in codes:
                a, e = node_series[(sk, c)]
                e2 = e[:52] + [x if i < cut else 0.0 for i, x in enumerate(e[52:])]
                node_series[(sk, c)] = (a, e2)
        if it["sku"] == "AUT-BRK-ACT-C":
            for c in codes:
                a, e = node_series[(sk, c)]
                e2 = e[:52] + [x * (1 if i < 3 else 5.5) for i, x in enumerate(e[52:])]
                node_series[(sk, c)] = (a, e2)
        # emit rows
        bias = float(rng.choice([-0.04, 0.0, 0.03, 0.08, 0.18], p=[0.15, 0.3, 0.25, 0.2, 0.1]))
        v["bias"] = bias
        for n in nodes:
            a, e = node_series[(sk, n["code"])]
            for i, w in enumerate(ctx.hist_weeks):
                if a[i] > 0 or it["pat"] in ("smooth", "erratic", "seasonal", "seasonal_winter", "trend_up", "declining", "spike"):
                    demand_rows.append(dict(item_id=o.id, location_id=n["obj"].id, period_start=w, granularity="W", qty=float(a[i]),
                                            channel=n["extra"].get("channel"), source_system="DEMO", status="ACTIVE", censored=False))
            for i, w in enumerate(ctx.all_weeks):
                if i < 26:
                    continue
                base_e = e[i]
                if it["pat"] == "spike" and i < 52:
                    base_e = e[max(i - 5, 0)]
                q = base_e * (1 + bias) * (1 + rng.normal(0, 0.06 if i < 52 else 0.0))
                if i < 52 and it["pat"] == "spike":
                    q = e[0] * (1 + bias)
                sig = 0.5 if it["pat"] in ("intermittent", "lumpy") else 0.15
                fc_rows.append(dict(item_id=o.id, location_id=n["obj"].id, period_start=w, granularity="W", qty=float(max(q, 0)),
                                    forecast_type="BASELINE", source="FIT", p10=float(max(q * (1 - 1.28 * sig), 0)), p90=float(q * (1 + 1.28 * sig)),
                                    version="FIT-2025.09", model_name="ETS/ML ensemble", issued_at=ctx.cur_w - timedelta(weeks=1), source_system="FIT",
                                    status="ACTIVE"))
                if i >= 52 and it["crit"] in ("Critical", "High") and i < 60:
                    fc_rows.append(dict(item_id=o.id, location_id=n["obj"].id, period_start=w, granularity="W",
                                        qty=float(max(q * 1.03, 0)), forecast_type="CONSENSUS", source="ERP", version="S&OP-Sep",
                                        model_name="Consensus", issued_at=ctx.cur_w, source_system="ERP", status="ACTIVE"))
                if it.get("promo") and 54 <= i <= 57 and n["w"] > 0:
                    fc_rows.append(dict(item_id=o.id, location_id=n["obj"].id, period_start=w, granularity="W",
                                        qty=float(q * 1.45), forecast_type="ADJUSTED", source="MANUAL", version="promo-adj", model_name="Manual uplift",
                                        issued_at=ctx.cur_w, source_system="ICT", status="ACTIVE"))
        v["nodes"] = nodes
        if it.get("promo"):
            for n in nodes:
                if n["w"] > 0:
                    db.session.add(Promotion(name=f"{it['desc']} festive promo", item_id=o.id, location_id=n["obj"].id,
                                             start_date=ctx.cur_w + timedelta(weeks=2), end_date=ctx.cur_w + timedelta(weeks=5),
                                             uplift_pct=45.0, promo_type="PRICE_OFF", source_system="DEMO"))
    db.session.bulk_insert_mappings(Demand, demand_rows)
    db.session.bulk_insert_mappings(Forecast, fc_rows)
    ctx.summary["demand_rows"] += len(demand_rows)
    ctx.summary["forecast_rows"] += len(fc_rows)
    db.session.flush()

    # stock, lots, sourcing, lead times ---------------------------------------------------------------------------
    port_items = {sk for sk, v in items.items() if v["spec"].get("port") == "Nhava Sheva (JNPT)" and spec["suppliers"][v["spec"]["sup"] or 0][3] == "China"}
    for sk, v in items.items():
        it, o = v["spec"], v["obj"]
        nodes = v["nodes"]
        for n in nodes:
            a, e = node_series[(sk, n["code"])]
            d_week = float(np.mean(a[-13:])) if any(a) else 0.0
            d_day = d_week / 7.0
            parent = locs.get(n["parent"]) if n["parent"] in {x["code"] for x in nodes} else None
            # sourcing
            if parent:
                lt = float(spec["ship_days"] + (n["echelon"] - 2 if n["echelon"] > 2 else 0) * 0.5)
                db.session.add(ItemLocationSource(item_id=o.id, location_id=n["obj"].id, source_location_id=parent["obj"].id, lead_time_days=max(lt, 1.0),
                                                  source_system="DEMO"))
                node_lt = max(lt, 1.0)
            elif it["src"] == "MAKE":
                db.session.add(ItemLocationSource(item_id=o.id, location_id=n["obj"].id, lead_time_days=float(it["lt"]), source_system="DEMO"))
                node_lt = float(it["lt"])
            else:
                node_lt = float(it["lt"])
            cover = spec["stock_cover"].get(n["type"], 20)
            factor = float(np.clip(rng.lognormal(0.0, 0.42), 0.18, 3.2))
            # planted stories -------------------------------------------------------------------------------------
            if sk in port_items and not parent:
                factor = 0.28
            if sk in ("FMC-BEV-COLA-1L",) and n["code"] == "FMC-RDC-W":
                factor = 6.5
            if sk in ("FMC-BEV-COLA-1L",) and n["code"] == "FMC-RDC-S":
                factor = 0.22
            if sk == "PHM-TAB-ATOR20" and n["code"] == "PHM-RDC-E":
                factor = 6.0
            if sk == "PHM-TAB-ATOR20" and n["code"] == "PHM-RDC-W":
                factor = 0.2
            if sk == "RET-ACC-EARB":
                factor = 0.35
            if sk == "RET-APP-SCARF":
                factor = 8.0
            if sk == "AUT-BRK-ACT-B":
                factor = 3.2
            if sk == "ELE-MEM-DDR4" or sk == "ELE-IC-OLD-PHY":
                factor = 2.6
            if sk == "MFG-RM-MOTOR":
                factor = 0.5
            if sk in ("SPR-PCB-OLD", "SPR-OLD-COUPL", "PHM-TAB-LEGACY"):
                factor = 1.0
            target = d_day * (cover + node_lt * 0.3) * factor
            if it["pat"] in ("intermittent", "lumpy"):
                target = max(target, d_day * cover * 0.6 + (1 if rng.random() < 0.7 else 0))
            if it["pat"] == "dead":
                target = float(rng.integers(6, 40) * (3 if it["moq"] < 5 else 60)) if n["type"] in ("CDC", "RDC", "WAREHOUSE") else 0.0
            qty = float(max(0.0, round(target)))
            if it["mult"] and it["mult"] > 0 and qty > 20:
                qty = round(qty / it["mult"]) * it["mult"] if it["mult"] < qty / 4 else qty
            ctx.pair_stats[(sk, n["code"])] = {"d_day": d_day, "lt": node_lt, "qty": qty}
            _make_stock(ctx, spec, v, n, qty, d_day, node_lt)
    # lead-time observations + orders ------------------------------------------------------------------------
    _lead_times_and_orders(ctx, spec, sup, items, locs, custs, port_items, node_series)
    ctx.summary["items"] += len(items)
    ctx.summary["locations"] += len(locs)
    ctx.summary["suppliers"] += len(sup)


def _make_stock(ctx: Ctx, spec, v, n, qty, d_day, node_lt):
    rng, today = ctx.rng, ctx.today
    it, o, loc = v["spec"], v["obj"], n["obj"]
    if qty <= 0:
        return
    old_days = int(rng.integers(3, 55))
    if it["pat"] == "dead" or it.get("life") == "OBSOLETE":
        old_days = int(rng.integers(420, 700))
    elif it["pat"] == "declining" and rng.random() < 0.6:
        old_days = int(rng.integers(120, 260))
    when0 = datetime.combine(today - timedelta(days=old_days), datetime.min.time()) + timedelta(hours=9)
    if not it.get("lot"):
        states = [("UNRESTRICTED", qty)]
        if it["crit"] in ("Critical", "High") and rng.random() < 0.10 and qty > 20:
            blk = round(qty * 0.05)
            states = [("UNRESTRICTED", qty - blk), ("BLOCKED", blk)]
        if it.get("wip") and n["type"] == "PLANT":
            states = [("UNRESTRICTED", round(qty * 0.4)), ("WIP", round(qty * 0.6))]
        if spec["code"] == "RETAIL" and rng.random() < 0.12 and qty > 30:
            r = round(qty * 0.04)
            states = [("UNRESTRICTED", qty - r), ("RETURNED", r)]
        for st, q in states:
            ctx.txn(item_id=o.id, location_id=loc.id, lot_id=None, type="OPENING", qty=q, state_to=st, at=when0, reason="Opening balance (demo)",
                    ref_type="OPENING", ref_id=None, supplier_id=None)
        if it.get("serial") and qty < 12:
            for k in range(int(qty)):
                sn = SerialNumber(item_id=o.id, serial_no=f"{o.sku[-6:]}-{loc.code[-4:]}-{int(rng.integers(10000, 99999))}{k}", location_id=loc.id,
                                  source_system="DEMO")
                db.session.add(sn)
        return
    # lot-tracked: two lots with different ages
    shelf = it.get("shelf") or 365
    n_lots = 2 if qty > 40 else 1
    splits = [0.6, 0.4] if n_lots == 2 else [1.0]
    for k, frac in enumerate(splits):
        q = round(qty * frac)
        if q <= 0:
            continue
        age = int(rng.integers(int(shelf * 0.12) + 5, int(shelf * 0.45) + 10)) if k == 0 else int(rng.integers(2, 18))
        state = "UNRESTRICTED"
        quality = "RELEASED"
        # --- planted expiry / quality stories
        if o.sku == "FMC-BEV-JUICE-200" and n["code"] == "FMC-RDC-W" and k == 0:
            age = shelf - 18
        if o.sku == "FMC-SNK-CHIP-50" and n["code"] == "FMC-RDC-S" and k == 0:
            age = shelf - 25
        if o.sku == "PHM-INJ-VACC" and n["code"] == "PHM-RDC-E" and k == 0:
            age = shelf + 5
            q = max(q, 300)
        if o.sku == "PHM-INJ-INSUL" and n["code"] == "PHM-3PL-N" and k == 0:
            age = shelf - 40
        if o.sku == "PHM-INJ-INSUL" and n["code"] == "PHM-CDC-01" and k == 1:
            state, quality = "QUARANTINED", "QUARANTINE"
            age = 6
        if o.sku == "PHM-TAB-METF500" and n["code"] == "PHM-CDC-01" and k == 1:
            state, quality = "BLOCKED", "REJECTED"
            age = 12
        if it.get("life") == "OBSOLETE":
            age = int(rng.integers(420, 600))
        received = today - timedelta(days=age)
        lot = Lot(item_id=o.id, lot_no=f"{o.sku[-7:].replace('-', '')}-{received.strftime('%y%W')}-{ctx.next_lot():04d}", kind="BATCH" if spec["code"] == "PHARMA" else "LOT",
                  supplier_id=None, manufacture_date=received - timedelta(days=int(rng.integers(3, 12))), received_date=received,
                  quality_status=quality, source_system="DEMO")
        lot.expiry_date = (lot.manufacture_date + timedelta(days=shelf)) if it.get("shelf") else None
        db.session.add(lot)
        db.session.flush()
        ctx.txn(item_id=o.id, location_id=loc.id, lot_id=lot.id, type="OPENING", qty=float(q), state_to=state,
                at=datetime.combine(received, datetime.min.time()) + timedelta(hours=10), reason="Opening balance (demo)",
                ref_type="OPENING", ref_id=None, supplier_id=None)


def _rand_date(rng, today, lo, hi):
    return today + timedelta(days=int(rng.integers(lo, hi + 1)))


def _lead_times_and_orders(ctx: Ctx, spec, sup, items, locs, custs, port_items, node_series):
    rng, today = ctx.rng, ctx.today
    code = spec["code"]
    po_seq = ctx.summary["pos"] + 1
    # ---- lead-time observations (receipt history) per supplier/item/dest
    obs = []
    for sk, v in items.items():
        it, o = v["spec"], v["obj"]
        if it["src"] != "BUY":
            continue
        s = sup[it["sup"]]
        ctop = [n for n in v["nodes"] if not (n["parent"] in {x["code"] for x in v["nodes"]})]
        for n in ctop:
            k_obs = int(rng.integers(9, 22))
            p_on_time = s._rel if s.code != "FMC-S-PACK" else 0.35
            late_hi = 1.45 + (1 - s._rel) * 1.2
            for j in range(k_obs):
                on_time = rng.random() < p_on_time
                lt = it["lt"] * (rng.uniform(0.86, 1.04) if on_time else rng.uniform(1.12, late_hi if s.code != "FMC-S-PACK" else 1.95))
                lt = max(1.0, lt)
                recv = today - timedelta(days=int(rng.integers(5, 330)))
                qo = float(it["moq"] * rng.integers(1, 4))
                short = rng.random() > s._rel + 0.05
                obs.append(dict(supplier_id=s.id, item_id=o.id, dest_location_id=n["obj"].id, origin=s.city, lane=it.get("lane") or f"{s.city}→{n['obj'].city}",
                                mode=it.get("mode", "ROAD"), po_ref=f"H{po_seq:05d}-{j}", ordered_date=recv - timedelta(days=int(round(lt))), received_date=recv,
                                lead_time_days=round(lt, 1), promised_days=float(it["lt"]), qty_ordered=qo,
                                qty_received=round(qo * (0.85 if short else 1.0)), rejected_qty=round(qo * (s.quality_rejection_rate if rng.random() < 0.3 else 0.0)),
                                expedited=bool(rng.random() < (1 - s._rel) * 0.5), source_system="DEMO", status="ACTIVE"))
        # internal replenishment observations (transfers)
        for n in v["nodes"]:
            if n["parent"] in {x["code"] for x in v["nodes"]}:
                pl = locs[n["parent"]]["obj"]
                base = max(spec["ship_days"] + (n["echelon"] - 2 if n["echelon"] > 2 else 0) * 0.5, 1.0)
                for j in range(int(rng.integers(8, 16))):
                    lt = base * float(rng.lognormal(0.05, 0.25))
                    recv = today - timedelta(days=int(rng.integers(3, 200)))
                    obs.append(dict(supplier_id=None, item_id=o.id, dest_location_id=n["obj"].id, origin=pl.code, lane=f"{pl.code}->{n['code']}", mode="ROAD",
                                    po_ref=f"T{j}", ordered_date=recv - timedelta(days=int(round(lt))), received_date=recv, lead_time_days=round(max(lt, 0.5), 1),
                                    promised_days=float(base), qty_ordered=100.0, qty_received=100.0, rejected_qty=0.0, expedited=False, source_system="DEMO",
                                    status="ACTIVE"))
    db.session.bulk_insert_mappings(LeadTimeObservation, obs)
    ctx.summary["lead_time_obs"] += len(obs)

    # ---- purchase orders + shipments
    for sk, v in items.items():
        it, o = v["spec"], v["obj"]
        if it["src"] != "BUY":
            continue
        s = sup[it["sup"]]
        codes = {x["code"] for x in v["nodes"]}
        for n in [x for x in v["nodes"] if x["parent"] not in codes]:
            st = ctx.pair_stats.get((sk, n["code"]))
            if not st:
                continue
            d_week = st["d_day"] * 7
            if d_week <= 0:
                continue
            q = max(float(it["moq"]), round(d_week * 3.5 / max(it["mult"], 1)) * it["mult"])
            n_po = 2 if it["lt"] > 20 or rng.random() < 0.35 else 1
            for k in range(n_po):
                state_roll = rng.random()
                promised = today + timedelta(days=int(it["lt"] * rng.uniform(0.15, 0.95)) + 2 * k)
                eta = promised
                in_transit = 0.0
                delay_reason = None
                delay = 0
                if state_roll < 0.55:
                    in_transit = q
                    promised = today + timedelta(days=int(rng.integers(2, 14)))
                    eta = promised
                elif state_roll > 0.87:
                    promised = today - timedelta(days=int(rng.integers(2, 12)))
                    eta = today + timedelta(days=int(rng.integers(1, 9)))
                    delay_reason = "Supplier production delay"
                    in_transit = 0.0
                if sk in port_items:
                    in_transit = q
                    promised = today + timedelta(days=int(rng.integers(3, 7)) + k)
                    delay = 9
                    eta = promised + timedelta(days=delay)
                    delay_reason = "Port congestion at Nhava Sheva (JNPT)"
                if s.code == "FMC-S-PACK" and k == 0:
                    promised = today - timedelta(days=9)
                    eta = today + timedelta(days=4)
                    delay_reason = "Supplier capacity shortage (resin allocation)"
                    in_transit = 0.0
                if s.code == "SPR-S-OEM1" and k == 0 and rng.random() < 0.7:
                    eta = promised + timedelta(days=int(rng.integers(6, 20)))
                    delay_reason = "OEM factory backlog"
                    delay = (eta - promised).days
                num = f"PO-{code[:3]}-{po_seq:05d}"
                po_seq += 1
                po = PurchaseOrder(po_number=num, supplier_id=s.id, dest_location_id=n["obj"].id, order_date=today - timedelta(days=int(rng.integers(3, int(it["lt"]) + 3))),
                                   promised_date=promised, eta_date=eta, mode=it.get("mode", "ROAD"), lane=it.get("lane"), status="OPEN",
                                   delay_reason=delay_reason, expedited=False, source_system="DEMO")
                db.session.add(po)
                db.session.flush()
                line = PurchaseOrderLine(po_id=po.id, line_no=1, item_id=o.id, qty_ordered=q, qty_received=0.0, qty_in_transit=in_transit,
                                         unit_price=it["cost"], promised_date=promised, eta_date=eta, source_system="DEMO")
                db.session.add(line)
                db.session.flush()
                ctx.summary["pos"] += 1
                if in_transit > 0:
                    mode = it.get("mode", "ROAD")
                    dist = route_km(s.lat, s.lon, n["obj"].lat, n["obj"].lon, mode)
                    shp = Shipment(shipment_no=f"SHP-{code[:3]}-{po_seq:05d}", ref_type="PO", ref_number=num, supplier_id=s.id, carrier_id=ctx.carriers[mode].id,
                                   dest_location_id=n["obj"].id, origin_name=s.city, mode=mode, lane=it.get("lane") or f"{s.city}→{n['obj'].city}",
                                   port=it.get("port"), dispatch_date=today - timedelta(days=int(rng.integers(1, 10))), promised_date=promised, eta_date=eta,
                                   delay_days=float(delay), delay_reason=delay_reason if delay else None, weight_kg=q * (it["weight"] or 1),
                                   distance_km=dist, tracking=f"TRK{int(rng.integers(10**8, 10**9))}",
                                   status="DELAYED" if delay else "IN_TRANSIT", source_system="DEMO")
                    db.session.add(shp)
                    db.session.flush()
                    db.session.add(ShipmentLine(shipment_id=shp.id, item_id=o.id, qty=in_transit, po_line_id=line.id))
                    ctx.summary["shipments"] += 1

    # ---- production orders
    for sk, v in items.items():
        it, o = v["spec"], v["obj"]
        if it["src"] != "MAKE":
            continue
        for n in v["nodes"]:
            if n["type"] != "PLANT":
                continue
            st = ctx.pair_stats.get((sk, n["code"]))
            if not st or st["d_day"] <= 0:
                continue
            for k in range(2):
                qty = max(float(it["moq"]), round(st["d_day"] * 7 * rng.uniform(0.6, 1.3) / max(it["mult"], 1)) * max(it["mult"], 1))
                due = today + timedelta(days=int(rng.integers(3, 12)) + 6 * k)
                po = ProductionOrder(order_number=f"MO-{code[:3]}-{ctx.summary['mos'] + 1:05d}", item_id=o.id, location_id=n["obj"].id, qty=qty,
                                     qty_completed=round(qty * 0.25) if k == 0 else 0.0, start_date=due - timedelta(days=int(it["lt"])), due_date=due,
                                     priority=int(rng.integers(1, 4)), status="RELEASED", bom_revision=o.revision, source_system="DEMO")
                db.session.add(po)
                ctx.summary["mos"] += 1
    # ---- sales orders (history + open)
    cust_rows = {c.code: c for c in Customer.query.filter(Customer.industry == code).all()}
    cust_list = list(cust_rows.values())
    hist_lines, open_so = [], []
    so_seq = ctx.summary["sos"] + 1
    for sk, v in items.items():
        it, o = v["spec"], v["obj"]
        if it.get("nofg") or it.get("itype") in ("RAW", "COMPONENT", "PACKAGING", "SEMI") or (code == "AUTOMOTIVE" and it["src"] == "BUY") \
                or (code == "ELECTRONICS" and it["src"] == "BUY"):
            continue
        for n in v["nodes"]:
            if n["w"] <= 0 and n["type"] not in ("PLANT", "WAREHOUSE", "RDC"):
                continue
            if code in ("AUTOMOTIVE", "ELECTRONICS") and n["type"] != "PLANT":
                continue
            if code == "MANUFACTURING" and n["type"] == "PLANT":
                continue
            st = ctx.pair_stats.get((sk, n["code"]))
            if not st or st["d_day"] <= 0:
                continue
            dw = st["d_day"] * 7
            for j in range(9):
                cust = cust_list[int(rng.integers(0, len(cust_list)))]
                req = today - timedelta(days=int(rng.integers(4, 90)))
                qo = max(1.0, round(dw * rng.uniform(0.25, 1.2)))
                full = rng.random() > 0.09
                sh = qo if full else round(qo * rng.uniform(0.5, 0.95))
                ontime = rng.random() > 0.08
                so = SalesOrder(so_number=f"SO-{code[:3]}-{so_seq:06d}", customer_id=cust.id, ship_from_location_id=n["obj"].id, order_date=req - timedelta(days=3),
                                requested_date=req, promised_date=req, priority=cust.priority, channel=n["extra"].get("channel", "B2B"), status="SHIPPED", source_system="DEMO")
                so_seq += 1
                db.session.add(so)
                db.session.flush()
                db.session.add(SalesOrderLine(so_id=so.id, line_no=1, item_id=o.id, qty_ordered=qo, qty_shipped=sh, unit_price=float(o.selling_price or o.unit_cost),
                                              requested_date=req, ship_date=req if ontime else req + timedelta(days=int(rng.integers(1, 5))),
                                              delivered_date=(req + timedelta(days=2)) if ontime else req + timedelta(days=int(rng.integers(3, 8))), source_system="DEMO", status="SHIPPED"))
            for j in range(int(rng.integers(1, 4))):
                cust = cust_list[int(rng.integers(0, len(cust_list)))]
                due = today + timedelta(days=int(rng.integers(0, 20)))
                qo = max(1.0, round(dw * rng.uniform(0.25, 0.9)))
                if sk == "FMC-BEV-COLA-1L" and n["code"] == "FMC-RDC-S" and j == 0:
                    due, qo = today - timedelta(days=6), round(dw * 0.8)
                so = SalesOrder(so_number=f"SO-{code[:3]}-{so_seq:06d}", customer_id=cust.id, ship_from_location_id=n["obj"].id, order_date=today - timedelta(days=int(rng.integers(1, 8))),
                                requested_date=due, promised_date=due, priority=cust.priority, channel=n["extra"].get("channel", "B2B"), status="OPEN", source_system="DEMO")
                so_seq += 1
                db.session.add(so)
                db.session.flush()
                db.session.add(SalesOrderLine(so_id=so.id, line_no=1, item_id=o.id, qty_ordered=qo, qty_shipped=0.0, unit_price=float(o.selling_price or o.unit_cost),
                                              requested_date=due, source_system="DEMO", status="OPEN"))
    ctx.summary["sos"] = so_seq - 1
    # ---- open transfers between nodes
    to_seq = ctx.summary["tos"] + 1
    for sk, v in items.items():
        it, o = v["spec"], v["obj"]
        if it.get("nofg"):
            continue
        codes = {x["code"]: x for x in v["nodes"]}
        for n in v["nodes"]:
            if n["parent"] in codes and rng.random() < 0.18:
                st = ctx.pair_stats.get((sk, n["code"]))
                if not st or st["d_day"] <= 0:
                    continue
                par = codes[n["parent"]]
                qty = max(1.0, round(st["d_day"] * 7 * rng.uniform(1.0, 2.5)))
                status = "IN_TRANSIT" if rng.random() < 0.7 else "OPEN"
                eta = today + timedelta(days=int(rng.integers(1, 6)))
                db.session.add(TransferOrder(to_number=f"TO-{code[:3]}-{to_seq:05d}", item_id=o.id, from_location_id=par["obj"].id, to_location_id=n["obj"].id,
                                             qty=qty, ship_date=today - timedelta(days=1) if status == "IN_TRANSIT" else today + timedelta(days=1),
                                             eta_date=eta, promised_date=eta, status=status, mode="ROAD", source_system="DEMO"))
                to_seq += 1
                ctx.summary["tos"] += 1
    # calendar
    db.session.add(CalendarEvent(name=f"{code} annual maintenance shutdown", event_type="SHUTDOWN", start_date=today + timedelta(days=52),
                                 end_date=today + timedelta(days=59), source_system="DEMO"))
    db.session.flush()
    # ---- BOM-driven trace lots (lineage) for FMCG and PHARMA
    if code in ("FMCG", "PHARMA"):
        _trace_story(ctx, spec, items, locs, cust_list)


def _trace_story(ctx: Ctx, spec, items, locs, custs):
    """Batch genealogy: supplier receipt of raw lot → consumption in production order → FG lot → transfers → customer shipment."""
    today = ctx.today
    parent_sku, comp_sku, _ = spec["bom"][0]
    fg, rm = items[parent_sku], items[comp_sku]
    if not (fg["obj"].lot_tracked and rm["obj"].lot_tracked):
        return
    plant = next(n for n in locs.values() if n["type"] == "PLANT")
    cdc = next(n for n in locs.values() if n["type"] == "CDC")
    rdc = next((n for n in locs.values() if n["type"] in ("RDC", "3PL")), cdc)
    sup_id = Supplier.query.filter_by(code=spec["suppliers"][rm["spec"]["sup"] or 0][0]).first().id
    d0 = today - timedelta(days=40)
    def mk(item, tag, days_ago, shelf):
        rec = today - timedelta(days=days_ago)
        lot = Lot(item_id=item["obj"].id, lot_no=tag, kind="BATCH" if spec["code"] == "PHARMA" else "LOT", manufacture_date=rec - timedelta(days=2), received_date=rec,
                  expiry_date=rec - timedelta(days=2) + timedelta(days=shelf) if shelf else None, quality_status="RELEASED", source_system="DEMO",
                  supplier_id=sup_id if item is rm else None)
        db.session.add(lot)
        db.session.flush()
        return lot
    rm_lot = mk(rm, f"TRACE-RM-{spec['code'][:3]}-01", 45, rm["spec"].get("shelf", 365))
    fg_lot = mk(fg, f"TRACE-FG-{spec['code'][:3]}-01", 30, fg["spec"].get("shelf", 365))
    at = lambda d: datetime.combine(today - timedelta(days=d), datetime.min.time()) + timedelta(hours=11)
    mo = f"MO-TRACE-{spec['code'][:3]}"
    ctx.txn(item_id=rm["obj"].id, location_id=plant["obj"].id, lot_id=rm_lot.id, type="RECEIPT", qty=8000.0, at=at(45), reason="Receipt from supplier", ref_type="PO",
            ref_id="PO-TRACE-001", supplier_id=sup_id)
    ctx.txn(item_id=rm["obj"].id, location_id=plant["obj"].id, lot_id=rm_lot.id, type="PROD_CONSUMPTION", qty=5000.0, at=at(31), reason="Consumed in production",
            ref_type="PROD", ref_id=mo)
    ctx.txn(item_id=fg["obj"].id, location_id=plant["obj"].id, lot_id=fg_lot.id, type="PROD_COMPLETION", qty=40000.0, at=at(30), reason="Production completion",
            ref_type="PROD", ref_id=mo)
    ctx.txn(item_id=fg["obj"].id, location_id=plant["obj"].id, lot_id=fg_lot.id, type="TRANSFER", qty=26000.0, to_location_id=cdc["obj"].id, at=at(26),
            reason="Plant → central DC", ref_type="TO", ref_id="TO-TRACE-001")
    ctx.txn(item_id=fg["obj"].id, location_id=cdc["obj"].id, lot_id=fg_lot.id, type="TRANSFER", qty=9000.0, to_location_id=rdc["obj"].id, at=at(19),
            reason="Central DC → regional", ref_type="TO", ref_id="TO-TRACE-002")
    cu = custs[0]
    ctx.txn(item_id=fg["obj"].id, location_id=rdc["obj"].id, lot_id=fg_lot.id, type="SHIPMENT", qty=3500.0, at=at(12), reason="Customer shipment",
            ref_type="SO", ref_id="SO-TRACE-001", customer_id=cu.id)
    ctx.txn(item_id=fg["obj"].id, location_id=rdc["obj"].id, lot_id=fg_lot.id, type="SHIPMENT", qty=1200.0, at=at(6), reason="Customer shipment",
            ref_type="SO", ref_id="SO-TRACE-002", customer_id=custs[-1].id)
    ctx.txn(item_id=fg["obj"].id, location_id=rdc["obj"].id, lot_id=fg_lot.id, type="RETURN", qty=120.0, at=at(3), reason="Customer return (damaged carton)",
            ref_type="RMA", ref_id="RMA-TRACE-001", customer_id=custs[-1].id)


def _finalize_ledger(ctx: Ctx) -> None:
    """Apply queued movements in chronological order in memory; write balances + transactions with the on-hand chain."""
    on_states = {s["code"] for s in S.state_defs() if s["counts_on_hand"]}
    bal: dict[tuple, float] = defaultdict(float)
    last_mv: dict[tuple, datetime] = {}
    last_rc: dict[tuple, datetime] = {}
    oh: dict[tuple, float] = defaultdict(float)
    rows = []
    for t in sorted(ctx.txns, key=lambda t: (t["at"], t["_seq"])):
        typ, q = t["type"], t["qty"]
        eff = ledger.effects(typ, t["location_id"], t.get("to_location_id"), t.get("state_from"), t.get("state_to"), None, q)
        before = oh[(t["item_id"], t["location_id"])]
        for loc, st, dlt in eff:
            key = (t["item_id"], loc, t.get("lot_id"), st)
            bal[key] += dlt
            last_mv[key] = t["at"]
            if dlt > 0 and typ in ("OPENING", "RECEIPT", "PROD_COMPLETION", "TRANSFER", "RETURN"):
                last_rc[key] = t["at"]
            if st in on_states:
                oh[(t["item_id"], loc)] += dlt
        rows.append(dict(txn_id=uuid.uuid4().hex, txn_type=typ, item_id=t["item_id"], location_id=t["location_id"], to_location_id=t.get("to_location_id"),
                         lot_id=t.get("lot_id"), quantity=float(q), uom="EA", state_from=t.get("state_from"), state_to=t.get("state_to"),
                         on_hand_before=before, on_hand_after=oh[(t["item_id"], t["location_id"])], occurred_at=t["at"], ref_type=t.get("ref_type"),
                         ref_id=t.get("ref_id"), customer_id=t.get("customer_id"), supplier_id=t.get("supplier_id"), reason=t.get("reason"), actor="demo-loader",
                         source_system="DEMO", status="ACTIVE"))
    db.session.bulk_insert_mappings(InventoryTransaction, rows)
    bals = [dict(item_id=k[0], location_id=k[1], lot_id=k[2], state=k[3], quantity=round(v, 6), uom="EA", last_movement_at=last_mv.get(k),
                 last_receipt_at=last_rc.get(k), source_system="DEMO", status="ACTIVE") for k, v in bal.items() if abs(v) > 1e-9]
    db.session.bulk_insert_mappings(InventoryBalance, bals)
    ctx.summary["transactions"] += len(rows)
    ctx.summary["balances"] += len(bals)
    ctx.txns = []


def _capacity_tuning(ctx: Ctx) -> None:
    """Make 'tight' nodes breach capacity so the capacity rule has something to find."""
    on_states = {s["code"] for s in S.state_defs() if s["counts_on_hand"]}
    tot = defaultdict(float)
    for b in InventoryBalance.query.all():
        if b.state in on_states:
            tot[b.location_id] += b.quantity
    for spec in SPECS.values():
        for (c, *_rest, extra) in spec["locations"]:
            if extra.get("tight"):
                l = Location.query.filter_by(code=c).first()
                if l and tot.get(l.id):
                    l.capacity_units = round(tot[l.id] * 0.9)
    tight = {c for spec in SPECS.values() for (c, *_r, extra) in spec["locations"] if extra.get("tight")}
    # scale all other capacities so utilisation is realistic (40-85 %)
    for l in Location.query.all():
        if tot.get(l.id) and l.code not in tight:
            l.capacity_units = round(tot[l.id] / float(ctx.rng.uniform(0.4, 0.85)))


def _recon_data(ctx: Ctx) -> None:
    """ERP / WMS / 3PL / PHYSICAL balances for reconciliation, with planted discrepancy types."""
    rng = ctx.rng
    on_states = {s["code"] for s in S.state_defs() if s["counts_on_hand"]}
    onh: dict[tuple, float] = defaultdict(float)
    for b in InventoryBalance.query.all():
        if b.state in on_states:
            onh[(b.item_id, b.location_id)] += b.quantity
    items = {i.id: i for i in Item.query.all()}
    locs = {l.id: l for l in Location.query.all()}
    now = S.now()
    rows = []
    k = 0
    for (iid, lid), q in onh.items():
        it, lc = items[iid], locs[lid]
        k += 1
        r = rng.random()
        if r < 0.985:
            u = rng.random()
            qty = q if u < 0.93 else (q * float(rng.uniform(0.985, 1.015)) if u < 0.97 else q * float(rng.uniform(1.06, 1.16)))
            rows.append(ExternalBalance(system="ERP", sku_raw=it.sku, location_raw=lc.code, quantity=round(qty), uom="EA", last_sync=now - timedelta(hours=3),
                                        snapshot_at=now - timedelta(hours=3), source_system="ERP"))
        if lc.loc_type in ("WAREHOUSE", "CDC", "RDC", "DARK_STORE", "MFC") and rng.random() < 0.97:
            u = rng.random()
            qty = q if u < 0.94 else (q * float(rng.uniform(0.99, 1.01)) if u < 0.97 else q * float(rng.uniform(0.88, 0.95)))
            rows.append(ExternalBalance(system="WMS", sku_raw=it.sku, location_raw=lc.code, quantity=round(qty), uom="EA", last_sync=now - timedelta(hours=30),
                                        snapshot_at=now - timedelta(hours=30), source_system="WMS"))
        if lc.is_3pl:
            rows.append(ExternalBalance(system="3PL", sku_raw=it.sku, location_raw=lc.code, quantity=round(q * float(rng.uniform(0.985, 1.0))), uom="EA",
                                        last_sync=now - timedelta(hours=20), snapshot_at=now - timedelta(hours=20), source_system="3PL"))
        if rng.random() < 0.14:
            qty = q if rng.random() < 0.9 else q * float(rng.uniform(0.9, 1.06))
            rows.append(ExternalBalance(system="PHYSICAL", sku_raw=it.sku, location_raw=lc.code, quantity=round(qty), uom="EA", last_sync=now - timedelta(days=5),
                                        snapshot_at=now - timedelta(days=5), source_system="COUNT"))
    keys = list(onh.keys())
    # planted anomalies --------------------------------------------------------------------------------------------
    for (iid, lid) in [keys[i] for i in rng.choice(len(keys), size=min(8, len(keys)), replace=False)]:
        rows.append(ExternalBalance(system="ERP", sku_raw=items[iid].sku, location_raw=locs[lid].code, quantity=round(onh[(iid, lid)] * 1.18 + 5), uom="EA",
                                    last_sync=now - timedelta(hours=3), source_system="ERP"))     # duplicate key + variance
    (iid, lid) = keys[int(rng.integers(0, len(keys)))]
    rows.append(ExternalBalance(system="WMS", sku_raw=items[iid].sku, location_raw=locs[lid].code, quantity=-14.0, uom="EA", last_sync=now - timedelta(hours=30), source_system="WMS"))
    (iid, lid) = keys[int(rng.integers(0, len(keys)))]
    rows.append(ExternalBalance(system="ERP", sku_raw=items[iid].sku, location_raw=locs[lid].code, quantity=round(onh[(iid, lid)] / 24 + 1), uom="CASE", last_sync=now - timedelta(hours=3),
                                source_system="ERP"))
    rows.append(ExternalBalance(system="ERP", sku_raw="ZZ-UNKNOWN-SKU-1", location_raw=next(iter(locs.values())).code, quantity=120.0, uom="EA", last_sync=now - timedelta(hours=3),
                                source_system="ERP"))
    rows.append(ExternalBalance(system="WMS", sku_raw=items[keys[0][0]].sku, location_raw="WH-UNMAPPED-77", quantity=45.0, uom="EA", last_sync=now - timedelta(hours=30),
                                source_system="WMS"))
    db.session.add_all(rows)
    # stale timing mismatch: last sync older than a recent ledger movement
    ctx.summary["external_balances"] += len(rows)


def _assets_returns(ctx: Ctx) -> None:
    rng = ctx.rng
    locs = Location.query.all()
    for l in locs:
        if l.loc_type in ("CDC", "PLANT", "RDC", "WAREHOUSE") and rng.random() < 0.7:
            for typ, val in [("PALLET", 850), ("TOTE", 320), ("DUNNAGE", 1800), ("CONTAINER", 2600)]:
                if rng.random() < 0.55:
                    q = int(rng.integers(200, 3000))
                    db.session.add(ReusableAsset(pool_code=f"{typ[:3]}-{l.industry[:3]}", asset_type=typ, location_id=l.id, owner=l.industry, qty_available=q,
                                                 qty_in_transit=int(q * rng.uniform(0.02, 0.15)), qty_repair=int(q * rng.uniform(0.01, 0.08)),
                                                 qty_lost=int(q * rng.uniform(0.0, 0.05)), unit_value=val, condition="GOOD", source_system="DEMO"))
    items = Item.query.filter(Item.industry.in_(["RETAIL", "FMCG", "ELECTRONICS", "AUTOMOTIVE"])).all()
    custs = Customer.query.all()
    today = ctx.today
    for _ in range(70):
        it = items[int(rng.integers(0, len(items)))]
        loc = Location.query.filter_by(industry=it.industry).order_by(Location.echelon.desc()).first()
        if not loc:
            continue
        db.session.add(ReturnRecord(item_id=it.id, location_id=loc.id, customer_id=custs[int(rng.integers(0, len(custs)))].id, qty=float(rng.integers(1, 40)),
                                    return_date=today - timedelta(days=int(rng.integers(1, 80))), reason=str(rng.choice(["Damaged", "Wrong item", "Defective", "Excess", "Expired", "Size"])),
                                    disposition=str(rng.choice(["RESTOCK", "REPAIR", "REFURBISH", "RECYCLE", "SCRAP", "PENDING"], p=[0.35, 0.12, 0.1, 0.1, 0.13, 0.2])), source_system="DEMO"))
    ctx.summary["assets"] += ReusableAsset.query.count()


def _sync_status(ctx: Ctx) -> None:
    now = S.now()
    for src, hrs_ago, recs in [("ERP", 3, 5200), ("WMS", 30, 3100), ("TMS", 2, 610), ("3PL", 20, 420), ("FORECAST", 60, 9800), ("INVENTORY", 1, 2100)]:
        row = SyncStatus.query.filter_by(source=src).first()
        if not row:
            row = SyncStatus(source=src)
            db.session.add(row)
        row.last_sync = now - timedelta(hours=hrs_ago)
        row.records = recs
        row.note = "demo feed (synthetic)"


def _kpi_history(ctx: Ctx) -> None:
    """Synthetic 26-week trend of headline KPIs, walked backwards from today's measured values (flagged source=DEMO)."""
    from . import kpi_service
    from .snapshot import get_snapshot
    snap = get_snapshot(force=True)
    t = snap.totals()
    rng = ctx.rng
    cur = {"inventory_value": t["on_hand_value"], "excess_value": t["excess_value"], "available_value": t["available_value"],
           "in_transit_value": t["in_transit_value"], "carbon_kg": 0.0}
    k = kpi_service.compute_all(snap)
    cur["service_level"] = (k.get("service_level") or {}).get("value") or 0.95
    cur["dio"] = (k.get("dio") or {}).get("value") or 60
    cur["stockout_risk"] = (k.get("stockout_risk") or {}).get("value") or 0.05
    trend = {m: v for m, v in cur.items() if m != "carbon_kg"}
    walk = {m: 1.0 for m in trend}
    KpiSnapshot.query.filter_by(source="DEMO").delete()
    for wk in range(0, 27):
        d = ctx.today - timedelta(weeks=wk)
        for m, v in trend.items():
            if wk == 0:
                val = v
            else:
                walk[m] *= float(1 + rng.normal(0.004 if m in ("inventory_value", "dio", "excess_value") else -0.001, 0.02))
                val = v * walk[m]
            if m in ("service_level",):
                val = min(0.999, val)
            db.session.add(KpiSnapshot(as_of=d, metric=m, value=float(val), scope="ALL", source="DEMO" if wk else "MEASURED"))
    db.session.commit()
