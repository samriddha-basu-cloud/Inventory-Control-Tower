"""Core inventory engine: a pure function from PairInputs (one item at one location) to PairResult.

Nothing here touches the database or Flask, so the digital twin can copy PairInputs, mutate the copy and
re-run the very same maths without any risk to production data.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import numpy as np

from ..rules.thresholds import stockout_level
from ..utils.stats import adi_cv2, bias, cv, demand_profile, mean, std, wape
from . import expiry_service as exp
from . import inventory_service as inv
from . import lead_time_service as lts
from . import projection_service as proj
from . import replenishment_service as rep
from . import safety_stock_service as sss


# ------------------------------------------------------------------------------------------------------ config
@dataclass
class EngineConfig:
    service_level: float = 0.95
    ss_method: str = "combined"
    lt_basis: str = "observed_mean"
    use_observed_lt: bool = True
    min_lt_obs: int = 8
    holding_rate: float = 0.22
    ordering_cost: float = 1500.0
    review_days: float = 7
    default_lt_days: float = 7
    stats_window_weeks: int = 26
    horizon_weeks: int = 13
    lost_sale_fraction: float = 0.6
    default_margin_pct: float = 0.25
    assumed_lt_cv: float = 0.0
    include_in_transit: bool = True
    excess_dos: float = 90
    slow_days: float = 90
    nonmoving_days: float = 180
    obsolete_days: float = 365
    obsolescence_prob: dict = field(default_factory=lambda: {"EXCESS": 0.03, "SLOW": 0.10, "NON_MOVING": 0.40, "OBSOLETE": 1.0})
    stockout_thresholds: dict = field(default_factory=lambda: {"medium": 0.05, "high": 0.15, "critical": 0.35})
    expiry_thresholds: dict = field(default_factory=lambda: {"near_days": 60, "critical_days": 30, "near_pct": 0.35})
    confidence_weights: dict = field(default_factory=lambda: {"completeness": 0.25, "freshness": 0.20, "stability": 0.20,
                                                              "model": 0.20, "constraints": 0.15})
    freshness: float = 1.0
    freshness_note: str = "no sync data"
    states: list = field(default_factory=list)
    today: date = field(default_factory=date.today)


def config_from_settings() -> EngineConfig:
    from . import settings_service as S
    g = S.get
    return EngineConfig(
        service_level=g("engine.service_level"), ss_method=g("engine.ss_method"), lt_basis=g("engine.lt_basis"),
        use_observed_lt=g("engine.use_observed_lt"), min_lt_obs=g("engine.min_lt_obs"),
        holding_rate=g("engine.holding_rate"), ordering_cost=g("engine.ordering_cost"),
        review_days=g("engine.review_days"), default_lt_days=g("engine.default_lt_days"),
        stats_window_weeks=g("engine.stats_window_weeks"), horizon_weeks=g("engine.horizon_weeks"),
        lost_sale_fraction=g("engine.lost_sale_fraction"), default_margin_pct=g("engine.default_margin_pct"),
        assumed_lt_cv=g("engine.assumed_lt_cv"), include_in_transit=g("engine.position_includes_in_transit"),
        excess_dos=g("excess.dos_threshold"), slow_days=g("excess.slow_days"),
        nonmoving_days=g("excess.nonmoving_days"), obsolete_days=g("excess.obsolete_days"),
        obsolescence_prob=g("excess.obsolescence_prob"), stockout_thresholds=g("thresholds.stockout"),
        expiry_thresholds=g("thresholds.expiry"), confidence_weights=g("weights.confidence"),
        states=S.state_defs(), today=S.today(),
    )


# ------------------------------------------------------------------------------------------------------ inputs
@dataclass
class PairInputs:
    item_id: int
    location_id: int
    sku: str
    loc_code: str
    item: dict = field(default_factory=dict)
    loc: dict = field(default_factory=dict)
    stock: dict = field(default_factory=dict)              # state -> qty
    allocated: float = 0.0
    committed: float = 0.0
    reserved: float = 0.0
    backorder: float = 0.0
    inbound: list = field(default_factory=list)            # qty, eta_day, promised_day, in_transit, kind, ref, supplier_id, lane, mode, origin, shipment_no
    orders: list = field(default_factory=list)             # qty, due_day, priority, customer_id, ref, value
    requirements: list = field(default_factory=list)       # BOM dependent demand: qty, due_day, ref
    transfers_out: list = field(default_factory=list)      # qty, day
    hist_weekly: list = field(default_factory=list)        # oldest -> newest
    forecast_weekly: list = field(default_factory=list)    # current week onwards (effective forecast)
    forecast_type: str | None = None
    fc_pairs: list = field(default_factory=list)           # [(forecast, actual)] past weeks
    lt_obs: list = field(default_factory=list)
    lt_promised: list = field(default_factory=list)
    static_lt: float | None = None
    supplier_id: int | None = None
    source_loc_id: int | None = None
    moq: float | None = None
    multiple: float | None = None
    supplier_capacity_week: float | None = None
    policy: dict = field(default_factory=dict)             # merged safety-stock policy params
    repl: dict = field(default_factory=dict)               # merged replenishment policy params
    lots: list = field(default_factory=list)               # {lot_id, lot_no, qty, state, expiry, received, quality, supplier_id}
    days_since_movement: float | None = None
    days_since_receipt: float | None = None
    days_since_last_demand: float | None = None
    mode: str = "ROAD"
    distance_km: float = 0.0
    lt_overrides: dict = field(default_factory=dict)       # used by scenarios: {shift, scale}
    extra: dict = field(default_factory=dict)

    @property
    def key(self):
        return (self.item_id, self.location_id)


@dataclass
class PairResult:
    item_id: int
    location_id: int
    sku: str
    loc_code: str
    # demand
    d_mean: float = 0.0
    d_std: float = 0.0
    annual_demand: float = 0.0
    demand_basis: str = ""
    demand_profile: str = "UNKNOWN"
    hist_weeks: int = 0
    # lead time
    lt_static: float | None = None
    lt_plan: float = 0.0
    lt_sigma: float = 0.0
    lt_p90: float | None = None
    lt_note: str = ""
    lt_stats: dict = field(default_factory=dict)
    # safety stock / policy
    ss: float = 0.0
    ss_method: str = ""
    ss_method_note: str = ""
    ss_formula: str = ""
    ss_inputs: dict = field(default_factory=dict)
    service_level: float = 0.95
    z: float = 0.0
    rop: float = 0.0
    ltd: float = 0.0
    eoq: float = 0.0
    practical_q: float = 0.0
    max_level: float = 0.0
    # position
    pos: dict = field(default_factory=dict)
    days_supply: float | None = None
    # projection
    projection: dict = field(default_factory=dict)
    first_stockout_day: int | None = None
    stockout_date: date | None = None
    min_projected: float = 0.0
    expected_shortage_qty: float = 0.0
    pessimistic_stockout_day: int | None = None
    # risk
    stockout_prob: float = 0.0
    p_below_ss: float = 0.0
    exp_short: float = 0.0
    service_impact: float = 0.0
    risk_level: str = "LOW"
    days_to_stockout: float | None = None
    lost_sales_value: float = 0.0
    lost_margin: float = 0.0
    production_risk: float = 0.0
    # value
    unit_cost: float = 0.0
    price: float = 0.0
    on_hand_value: float = 0.0
    usable_value: float = 0.0
    in_transit_value: float = 0.0
    on_order_value: float = 0.0
    wip_value: float = 0.0
    quarantined_value: float = 0.0
    blocked_value: float = 0.0
    # excess & obsolescence
    inv_class: str = "NORMAL"
    excess_qty: float = 0.0
    excess_value: float = 0.0
    carrying_cost_year: float = 0.0
    obsolescence_exposure: float = 0.0
    at_risk_qty: float = 0.0
    at_risk_value: float = 0.0
    obsolete_qty: float = 0.0
    obsolete_value: float = 0.0
    # aging / expiry
    age_days: float | None = None
    oldest_days: float | None = None
    days_to_expiry: float | None = None
    expiry_status: str = "NONE"
    expired_qty: float = 0.0
    # recommendation
    rec: dict = field(default_factory=dict)
    # data quality
    issues: list = field(default_factory=list)
    confidence: float = 0.0
    data_quality: float = 0.0
    confidence_parts: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ------------------------------------------------------------------------------------------------------ helpers
def review_days_for(p: "PairInputs", cfg: "EngineConfig") -> float:
    """Review period: policy/repl override, else 1 day for pull systems (Kanban/JIT/JIS), else the configured default."""
    explicit = p.repl.get("review_days") or p.policy.get("review_days")
    if explicit:
        return float(explicit)
    if (p.repl.get("policy") or "").upper() in ("KANBAN", "JIT", "JIS"):
        return 1.0
    return float(cfg.review_days)


def _daily_from_weekly(weekly: list[float], days: int, fallback: float) -> list[float]:
    out: list[float] = []
    for w in weekly:
        out.extend([w / 7.0] * 7)
        if len(out) >= days:
            break
    tail = out[-1] if out else fallback
    return (out + [fallback if not weekly else tail] * days)[:days]


def demand_stats(p: PairInputs, cfg: EngineConfig, window_days: float) -> dict:
    hist = [float(x) for x in p.hist_weekly]
    win = hist[-cfg.stats_window_weeks:] if hist else []
    issues = []
    fw = int(math.ceil(max(window_days, 1) / 7.0))
    if p.forecast_weekly:
        mean_w = mean(p.forecast_weekly[:max(fw, 1)])
        basis = f"{p.forecast_type or 'forecast'} for next {fw} wk"
    elif win:
        mean_w = mean(win)
        basis = f"history mean ({len(win)} wk) - no forecast"
        issues.append("No forecast available: demand taken from history")
    else:
        mean_w, basis = 0.0, "no demand data"
        issues.append("No demand history")
    pairs = p.fc_pairs
    if len(pairs) >= 8:
        errs = [f - a for f, a in pairs]
        sigma_w = std(errs)
        sbasis = f"forecast error σ ({len(errs)} wk)"
    elif len(win) >= 8:
        sigma_w = std(win)
        sbasis = f"demand σ ({len(win)} wk)"
    elif win and mean_w > 0:
        sigma_w = 0.5 * mean_w
        sbasis = "assumed CV 0.5 (history < 8 wk)"
        issues.append("History under 8 weeks: variability assumed")
    else:
        sigma_w, sbasis = 0.0, "n/a"
    return {"d_mean": mean_w / 7.0, "d_std": sigma_w / math.sqrt(7.0), "basis": f"{basis}; {sbasis}",
            "annual": (sum(hist[-52:]) * (52 / max(min(len(hist), 52), 1)) if hist else mean_w * 52),
            "profile": demand_profile(win), "issues": issues}


def bootstrap_ltd(weekly: list[float], L_days: float, n: int = 2000, seed: int = 7) -> np.ndarray:
    """Empirical lead-time-demand distribution for intermittent demand: sum of ceil(L/7) weeks resampled."""
    if not weekly:
        return np.zeros(0)
    rng = np.random.default_rng(seed)
    k = max(1, int(math.ceil(L_days / 7.0)))
    samples = rng.choice(np.asarray(weekly, float), size=(n, k), replace=True).sum(axis=1)
    return samples * (L_days / (7.0 * k))


def _sigma_eta(p: PairInputs, ib: dict, lt_sigma: float) -> float:
    return lt_sigma * (0.5 if ib.get("in_transit") else 1.0)


# ------------------------------------------------------------------------------------------------------ main
def compute_pair(p: PairInputs, cfg: EngineConfig, with_projection: bool = True) -> PairResult:
    today = cfg.today
    r = PairResult(item_id=p.item_id, location_id=p.location_id, sku=p.sku, loc_code=p.loc_code)
    it = p.item
    cost = float(it.get("unit_cost") or 0.0)
    price = float(it.get("selling_price") or 0.0)
    r.unit_cost, r.price = cost, price
    issues = r.issues

    # lead time ------------------------------------------------------------------------------------------------
    st = lts.compute_stats(p.lt_obs, p.static_lt, cfg.min_lt_obs, p.lt_promised)
    L, sigL, note = lts.planning_lead_time(st, cfg.lt_basis, cfg.use_observed_lt, cfg.default_lt_days, cfg.assumed_lt_cv)
    shift, scale = p.lt_overrides.get("shift", 0.0), p.lt_overrides.get("scale", 1.0)
    if shift or scale != 1.0:
        L, sigL = L * scale + shift, sigL * scale
        note += f"; scenario adjusts lead time (×{scale:g}, +{shift:g} d)"
    r.lt_static, r.lt_plan, r.lt_sigma, r.lt_p90, r.lt_note, r.lt_stats = st.static, L, sigL, st.p90, note, st.to_dict()
    if not p.static_lt and not st.n:
        issues.append("No lead time (master or observed): default used")
    R = review_days_for(p, cfg)

    # demand ---------------------------------------------------------------------------------------------------
    ds = demand_stats(p, cfg, L + R)
    d, sd = ds["d_mean"], ds["d_std"]
    r.d_mean, r.d_std, r.annual_demand, r.demand_basis = d, sd, d * 365.0, ds["basis"]
    r.demand_profile, r.hist_weeks = ds["profile"], len(p.hist_weekly)
    issues += ds["issues"]
    if not cost:
        issues.append("Missing unit cost")

    # policy inputs --------------------------------------------------------------------------------------------
    sl = float(p.policy.get("service_level") or it.get("target_service_level") or cfg.service_level)
    method = p.policy.get("method") or cfg.ss_method
    r.service_level = sl
    e = rep.eoq(r.annual_demand, cfg.ordering_cost, cost, cfg.holding_rate)
    moq, mult = p.moq or it.get("moq"), p.multiple or it.get("order_multiple")
    pq = rep.practical_qty(e, moq, mult, p.repl.get("max_order_qty"))
    r.eoq, r.practical_q = e, pq["qty"]
    if moq is None:
        issues.append("MOQ not defined")

    # safety stock ---------------------------------------------------------------------------------------------
    ltd_samples = None
    note_ss = ""
    if r.demand_profile in ("INTERMITTENT", "LUMPY") and method not in ("basic", "empirical") and len(p.hist_weekly) >= 20:
        method, note_ss = "empirical", f"{r.demand_profile.lower()} demand: normal approximation invalid → empirical bootstrap"
    if method == "empirical":
        ltd_samples = bootstrap_ltd(p.hist_weekly[-52:], L)
    fixed = p.policy.get("fixed_qty")
    days_cover = p.policy.get("days_cover")
    if fixed is not None:
        ss_res = sss.SSResult("fixed", float(fixed), 0.0, 0.0, "SS = fixed quantity (policy override)", {"fixed": fixed})
    elif days_cover is not None:
        ss_res = sss.SSResult("days_cover", d * float(days_cover), 0.0, 0.0, "SS = d̄ × days of cover (policy override)",
                              {"days_cover": days_cover})
    else:
        ss_res = sss.safety_stock(method, d_mean=d, d_std=sd, L=L, L_std=sigL, service_level=sl, review_days=R,
                                  order_qty=r.practical_q or e, ltd_samples=ltd_samples)
        if ss_res.warning:
            note_ss += ss_res.warning
    r.ss, r.ss_method, r.ss_method_note, r.ss_formula = ss_res.ss, ss_res.method, note_ss, ss_res.formula
    r.ss_inputs = {**ss_res.inputs, "sigma_ltd": ss_res.sigma_ltd}
    r.z = ss_res.z
    rp = sss.reorder_point(d, L, r.ss)
    r.ltd, r.rop = rp["lead_time_demand"], rp["reorder_point"]
    r.max_level = r.rop + (r.practical_q or e)

    # position -------------------------------------------------------------------------------------------------
    in_transit = sum(i["qty"] for i in p.inbound if i.get("in_transit") and i.get("kind") in ("PO", "TO"))
    on_order = sum(i["qty"] for i in p.inbound if not i.get("in_transit") and i.get("kind") in ("PO", "TO", "PROD"))
    pos = inv.compute_position(p.stock, cfg.states, allocated=p.allocated, committed=p.committed, reserved=p.reserved,
                               in_transit=in_transit, on_order=on_order, backorder=p.backorder, safety_stock=r.ss,
                               include_in_transit=cfg.include_in_transit)
    r.pos = pos
    usable, on_hand = pos["usable_on_hand"], pos["on_hand"]
    r.days_supply = (usable / d) if d > 1e-9 else None
    if pos["over_allocated"]:
        issues.append("Over-allocated: claims exceed usable stock")
    if any(v < -1e-9 for v in p.stock.values()):
        issues.append("Negative inventory balance")
    r.on_hand_value = on_hand * cost
    r.usable_value = usable * cost
    r.in_transit_value = in_transit * cost
    r.on_order_value = on_order * cost
    r.wip_value = p.stock.get("WIP", 0.0) * cost
    r.quarantined_value = p.stock.get("QUARANTINED", 0.0) * cost
    r.blocked_value = (p.stock.get("BLOCKED", 0.0)) * cost

    # projection & stock-out risk ------------------------------------------------------------------------------
    H = int(cfg.horizon_weeks * 7)
    fc_daily = _daily_from_weekly(p.forecast_weekly, H, d)
    inbound_for_proj = [{**i, "sigma_days": _sigma_eta(p, i, sigL)} for i in p.inbound]
    pr = proj.project(usable, fc_daily, p.orders, p.requirements, inbound_for_proj, p.transfers_out, r.ss,
                      horizon_days=H, step_days=7, eta_mode="eta", backorder=p.backorder)
    r.projection = pr
    r.first_stockout_day = pr["first_stockout_day"]
    r.stockout_date = today + timedelta(days=pr["first_stockout_day"]) if pr["first_stockout_day"] is not None else None
    r.min_projected, r.expected_shortage_qty = pr["min_ending"], pr["expected_shortage"]
    T = max(L, 1.0)
    Ti = int(min(H, math.ceil(T)))
    dd = pr.get("daily_demand") or [d] * Ti
    demand_T = float(sum(dd[:Ti])) if dd else d * T
    dem_T_mean = max(demand_T, d * T) if not p.orders else demand_T
    sig_T = sd * math.sqrt(T)
    so = proj.stockout_probability(usable, inbound_for_proj, dem_T_mean, sig_T, T, r.ss, p.backorder)
    r.stockout_prob, r.p_below_ss, r.exp_short, r.service_impact = so["probability"], so["p_below_safety"], so["expected_shortage"], so["service_impact"]
    # pessimistic (P90-delayed) deterministic view
    pess = proj.project(usable, fc_daily, p.orders, p.requirements, inbound_for_proj, p.transfers_out, r.ss,
                        horizon_days=H, step_days=7, eta_mode="p90", backorder=p.backorder)
    r.pessimistic_stockout_day = pess["first_stockout_day"]
    fso = pr["first_stockout_day"]
    r.days_to_stockout = float(fso) if fso is not None else (
        (usable / d) if (d > 1e-9 and not p.inbound and usable / d < H) else None)
    r.risk_level = stockout_level(r.stockout_prob, r.days_to_stockout, L, cfg.stockout_thresholds)
    if d <= 1e-9 and not p.orders and not p.requirements:
        r.risk_level, r.stockout_prob = "LOW", 0.0
    margin = (price - cost) if price > cost else price * cfg.default_margin_pct
    r.lost_sales_value = r.exp_short * cfg.lost_sale_fraction * price
    r.lost_margin = r.exp_short * cfg.lost_sale_fraction * margin
    r.production_risk = r.exp_short * float(it.get("line_stop_cost_per_unit") or 0.0)

    # excess / slow / obsolete ---------------------------------------------------------------------------------
    life = it.get("lifecycle_status")
    eol_passed = bool(it.get("eol_day") is not None and it["eol_day"] <= 0)
    dsm = p.days_since_movement
    dsd = p.days_since_last_demand
    cls = "NORMAL"
    on_hand_stock = on_hand
    if on_hand_stock > 0:
        if life == "OBSOLETE" or (dsd is not None and dsd > cfg.obsolete_days) or (eol_passed and d < 1e-9):
            cls = "OBSOLETE"
        elif (dsd is not None and dsd > cfg.nonmoving_days) or (d <= 1e-9 and (dsm or 0) > cfg.nonmoving_days):
            cls = "NON_MOVING"
        elif (dsm is not None and dsm > cfg.slow_days) or (d > 0 and usable / d > cfg.excess_dos * 3):
            cls = "SLOW"
    threshold_qty = max(r.max_level, d * cfg.excess_dos)
    if cls in ("OBSOLETE", "NON_MOVING"):
        r.excess_qty = on_hand_stock
    else:
        r.excess_qty = max(0.0, usable - threshold_qty)
        if cls == "NORMAL" and r.excess_qty > 0:
            cls = "EXCESS"
    r.inv_class = cls
    r.excess_value = r.excess_qty * cost
    r.carrying_cost_year = r.excess_value * cfg.holding_rate
    r.obsolescence_exposure = r.excess_value * cfg.obsolescence_prob.get(cls, 0.0)
    if cls == "OBSOLETE":
        r.obsolete_qty, r.obsolete_value = on_hand_stock, on_hand_stock * cost

    # aging / expiry -------------------------------------------------------------------------------------------
    ages, qtys = [], []
    for l in p.lots:
        if l["qty"] > 0 and l.get("received") is not None:
            ages.append((today - l["received"]).days)
            qtys.append(l["qty"])
    if ages:
        r.age_days = float(np.average(ages, weights=qtys))
        r.oldest_days = float(max(ages))
    elif p.days_since_receipt is not None:
        r.age_days = r.oldest_days = float(p.days_since_receipt)
    dte = [ (l["expiry"] - today).days for l in p.lots if l["qty"] > 0 and l.get("expiry") ]
    if dte:
        r.days_to_expiry = float(min(dte))
        worst = min((l for l in p.lots if l["qty"] > 0 and l.get("expiry")), key=lambda l: l["expiry"])
        r.expiry_status = exp.expiry_info(worst["expiry"], None, it.get("shelf_life_days"), today, cfg.expiry_thresholds)["status"]
        r.expired_qty = sum(l["qty"] for l in p.lots if l["qty"] > 0 and l.get("expiry") and l["expiry"] < today)
        risk_lots = exp.expiry_at_risk([l for l in p.lots if l.get("state", "UNRESTRICTED") in ("UNRESTRICTED", "QUARANTINED")], d, today)
        r.at_risk_qty = sum(l["at_risk_qty"] for l in risk_lots)
        r.at_risk_value = r.at_risk_qty * cost
        r.obsolescence_exposure += r.at_risk_value if cls == "NORMAL" else 0.0

    # replenishment recommendation ----------------------------------------------------------------------------
    r.rec = _recommend(p, cfg, r, pr, fc_daily, L, R, moq, mult, on_order, in_transit)

    # confidence -----------------------------------------------------------------------------------------------
    _confidence(p, cfg, r, st, moq, mult)
    return r


def _recommend(p, cfg, r, pr, fc_daily, L, R, moq, mult, on_order, in_transit) -> dict:
    policy = (p.repl.get("policy") or "ROP").upper()
    H = len(pr.get("daily_ending") or [])
    usable = r.pos["usable_on_hand"]
    ctx = {"position": r.pos["position"], "d": r.d_mean, "L": L, "R": R, "ss": r.ss, "rop": r.rop, "eoq": r.practical_q or r.eoq,
           "review_due": True}
    if policy in ("MRP", "DRP"):
        n = H or int(cfg.horizon_weeks * 7)
        rec_daily = [0.0] * n
        for i in p.inbound:
            day = max(0, int(math.ceil(i["eta_day"])))
            if day < n:
                rec_daily[day] += i["qty"]
        dem = (pr.get("daily_demand") or [r.d_mean] * n)
        ctx["planned"] = rep.mrp_plan(usable, rec_daily, dem, r.ss, L, moq, mult, p.repl.get("max_order_qty"))
    if policy == "JIS":
        hz = int(p.repl.get("sequence_days", 5))
        req = sum(o["qty"] for o in p.orders if o["due_day"] <= hz) + sum(x["qty"] for x in p.requirements if x["due_day"] <= hz)
        ctx["sequenced_req"] = req
        ctx["usable_plus_inbound_in_horizon"] = usable + sum(i["qty"] for i in p.inbound if i["eta_day"] <= hz)
    if policy == "L4L":
        ending = pr.get("daily_ending") or []
        # L4L nets existing receipts: reuse the projection's own shortfall day
        fso = pr.get("first_below_ss_day")
        if fso is not None:
            need = max(r.ss - min(ending[fso:fso + 7] or [0]), 0)
            ctx["position"] = r.pos["position"]
            rr = {"policy": "L4L", "name": rep.POLICIES["L4L"]["name"], "rule": rep.POLICIES["L4L"]["rule"],
                  "triggered": (fso - L) <= max(R, 0), "raw_qty": need, "trigger_level": r.ss, "order_up_to": None,
                  "planned": None, "explanation": f"projected inventory falls below SS on day {fso}; net requirement {need:,.0f}"}
        else:
            rr = {"policy": "L4L", "name": rep.POLICIES["L4L"]["name"], "rule": rep.POLICIES["L4L"]["rule"],
                  "triggered": False, "raw_qty": 0.0, "trigger_level": r.ss, "order_up_to": None, "planned": None,
                  "explanation": "no projected shortfall inside the horizon"}
    else:
        rr = rep.recommend(policy, p.repl, ctx)
    pq = rep.practical_qty(rr["raw_qty"], moq, mult, p.repl.get("max_order_qty")) if rr["raw_qty"] > 0 else {"qty": 0.0, "notes": [], "violations": [], "theoretical": 0.0}
    qty = pq["qty"]
    lead = math.ceil(L)
    ending = pr.get("daily_ending") or []
    if rr["triggered"]:
        order_day = 0
    elif r.d_mean > 1e-9:
        order_day = int(max(0, math.floor((r.pos["position"] - r.rop) / r.d_mean)))
    else:
        order_day = None
    arrival_day = None if order_day is None else order_day + lead
    proj_at_arrival = None
    if arrival_day is not None and ending:
        proj_at_arrival = ending[min(arrival_day, len(ending) - 1)]
    T_no = L + R
    dem_no = r.d_mean * T_no
    so_no = proj.stockout_probability(r.pos["usable_on_hand"], [{**i, "sigma_days": r.lt_sigma} for i in p.inbound],
                                      dem_no, r.d_std * math.sqrt(max(T_no, 1)), T_no, r.ss, p.backorder)
    return {
        "policy": policy, "policy_name": rr["name"], "rule": rr["rule"], "triggered": bool(rr["triggered"] and qty > 0),
        "raw_qty": rr["raw_qty"], "qty": qty, "notes": pq["notes"], "violations": pq["violations"],
        "trigger_level": rr.get("trigger_level"), "order_up_to": rr.get("order_up_to"), "explanation": rr["explanation"],
        "order_in_days": order_day, "order_date": (cfg.today + timedelta(days=order_day)) if order_day is not None else None,
        "arrival_date": (cfg.today + timedelta(days=arrival_day)) if arrival_day is not None else None,
        "projected_at_arrival": proj_at_arrival,
        "post_receipt_inventory": (proj_at_arrival + qty) if (proj_at_arrival is not None and qty) else proj_at_arrival,
        "post_order_position": r.pos["position"] + qty,
        "risk_if_not_ordered": {"probability": so_no["probability"], "expected_shortage": so_no["expected_shortage"],
                                "window_days": T_no, "lost_sales_value": so_no["expected_shortage"] * cfg.lost_sale_fraction * r.price},
        "planned": rr.get("planned"), "cost": qty * r.unit_cost,
        "inputs": {"position": r.pos["position"], "available": r.pos["available"], "inbound": on_order + in_transit,
                   "safety_stock": r.ss, "rop": r.rop, "d_mean": r.d_mean, "lead_time": L, "review_days": R,
                   "moq": moq, "multiple": mult, "eoq": r.eoq, "expected_demand_lt": r.d_mean * (L + R)},
    }


def _confidence(p, cfg, r, st, moq, mult):
    w = cfg.confidence_weights
    it = p.item
    checks = {
        "unit cost": bool(it.get("unit_cost")),
        "lead time (master/observed)": bool(p.static_lt or st.n),
        "≥26 wk demand history": len(p.hist_weekly) >= 26,
        "forecast present": bool(p.forecast_weekly),
        "supplier/source known": bool(p.supplier_id or p.source_loc_id),
        "lead-time observations ≥ min": st.eligible,
    }
    comp = sum(checks.values()) / len(checks)
    missing = [k for k, v in checks.items() if not v]
    cvd = cv(p.hist_weekly[-26:]) if len(p.hist_weekly) >= 8 else 1.0
    cvd = 1.0 if math.isinf(cvd) else cvd
    cvl = (st.std / st.mean) if (st.eligible and st.mean) else 0.3
    stab = 0.5 * (1 - min(1.0, cvd / 1.5)) + 0.5 * (1 - min(1.0, cvl / 0.5))
    if len(p.fc_pairs) >= 8:
        fa = [f for f, _ in p.fc_pairs]
        aa = [a for _, a in p.fc_pairs]
        wp = wape(aa, fa)
        model = 1 - min(1.0, wp)
        mnote = f"forecast WAPE {wp:.0%} over {len(fa)} wk"
    else:
        model, mnote = 0.5, "no forecast-vs-actual history (neutral 50 %)"
    cons_checks = {"MOQ": moq is not None, "order multiple": mult is not None,
                   "supplier capacity": p.supplier_capacity_week is not None, "location capacity": bool(p.loc.get("capacity_units"))}
    cons = sum(cons_checks.values()) / len(cons_checks)
    parts = [
        {"name": "Data completeness", "score": comp, "weight": w["completeness"], "why": f"missing: {', '.join(missing) or 'nothing'}"},
        {"name": "Data freshness", "score": cfg.freshness, "weight": w["freshness"], "why": cfg.freshness_note},
        {"name": "Historical stability", "score": stab, "weight": w["stability"], "why": f"demand CV {cvd:.2f}, lead-time CV {cvl:.2f}"},
        {"name": "Model performance", "score": model, "weight": w["model"], "why": mnote},
        {"name": "Constraint completeness", "score": cons, "weight": w["constraints"],
         "why": "known: " + (", ".join(k for k, v in cons_checks.items() if v) or "none")},
    ]
    tw = sum(x["weight"] for x in parts) or 1.0
    r.confidence = sum(x["score"] * x["weight"] for x in parts) / tw
    r.data_quality = (comp * w["completeness"] + cfg.freshness * w["freshness"]) / ((w["completeness"] + w["freshness"]) or 1.0)
    r.confidence_parts = parts
