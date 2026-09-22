"""Pure-calculation tests: formulas are checked against hand-computed values."""
import math

import pytest

from app.services import abc_xyz_service as seg
from app.services import expiry_service as exp
from app.services import inventory_service as inv
from app.services import lead_time_service as lts
from app.services import projection_service as proj
from app.services import replenishment_service as rep
from app.services import safety_stock_service as sss
from app.rules import policies as pol
from app.rules.rule_engine import FormulaError, eval_condition, safe_eval, severity_for
from app.rules.thresholds import expiry_status, kpi_status, stockout_level
from app.utils import uom
from app.utils.stats import cv, z_from_service_level, z_from_loss, normal_loss

STATES = [dict(code="UNRESTRICTED", counts_on_hand=True, allocatable=True, counts_in_position=True),
          dict(code="QUARANTINED", counts_on_hand=True, allocatable=False, counts_in_position=False),
          dict(code="WIP", counts_on_hand=False, allocatable=False, counts_in_position=False)]


# ---- inventory balance / position ----------------------------------------------------------------------------------------
def test_inventory_position_formulas_no_double_counting():
    p = inv.compute_position({"UNRESTRICTED": 1000, "QUARANTINED": 200, "WIP": 300}, STATES, allocated=150, committed=50, reserved=20,
                             in_transit=400, on_order=250, backorder=30, safety_stock=100)
    assert p["on_hand"] == 1200                       # WIP is not warehouse on-hand
    assert p["usable_on_hand"] == 1000                # quarantined stock is not usable
    assert p["available"] == 1000 - 150 - 50 - 20     # claims reduce AVAILABLE, never on-hand
    assert p["net_available"] == p["available"] - 100
    assert p["position"] == 1000 + 400 + 250 - 220 - 30
    assert not p["over_allocated"]


def test_over_allocation_is_flagged():
    p = inv.compute_position({"UNRESTRICTED": 100}, STATES, allocated=150)
    assert p["over_allocated"] and p["available"] == -50


# ---- safety stock / ROP / EOQ ---------------------------------------------------------------------------------------------
def test_z_scores():
    assert z_from_service_level(0.95) == pytest.approx(1.6449, abs=1e-3)
    assert z_from_service_level(0.99) == pytest.approx(2.3263, abs=1e-3)


def test_safety_stock_methods_match_formulas():
    kw = dict(d_mean=100, d_std=20, L=9, L_std=2, service_level=0.95)
    z = 1.6448536
    assert sss.safety_stock("demand", **kw).ss == pytest.approx(z * 20 * 3, rel=1e-4)
    assert sss.safety_stock("leadtime", **kw).ss == pytest.approx(z * 100 * 2, rel=1e-4)
    assert sss.safety_stock("combined", **kw).ss == pytest.approx(z * math.sqrt(9 * 400 + 100 ** 2 * 4), rel=1e-4)
    assert sss.safety_stock("periodic", review_days=7, **kw).ss == pytest.approx(z * math.sqrt(16 * 400 + 100 ** 2 * 4), rel=1e-4)
    assert sss.safety_stock("continuous", **kw).ss == sss.safety_stock("combined", **kw).ss
    assert sss.safety_stock("basic", d_max=140, L_max=12, **kw).ss == pytest.approx(140 * 12 - 100 * 9)


def test_fill_rate_method_is_below_cycle_service_level_ss_for_large_orders():
    kw = dict(d_mean=100, d_std=20, L=9, L_std=2, service_level=0.95)
    fill = sss.safety_stock("service_level", order_qty=2000, **kw).ss
    csl = sss.safety_stock("combined", **kw).ss
    assert 0 <= fill < csl      # with big lots most demand is met from cycle stock, so the buffer needed for a 95 % FILL RATE is smaller


def test_unknown_method_and_bad_service_level_rejected():
    with pytest.raises(ValueError):
        sss.safety_stock("magic", d_mean=1, d_std=1, L=1)
    with pytest.raises(ValueError):
        sss.safety_stock("combined", d_mean=1, d_std=1, L=1, service_level=1.2)


def test_loss_function_inverse():
    assert normal_loss(z_from_loss(0.05)) == pytest.approx(0.05, abs=1e-6)


def test_reorder_point():
    r = sss.reorder_point(100, 9, 343.5)
    assert r["lead_time_demand"] == 900 and r["reorder_point"] == pytest.approx(1243.5)


def test_eoq_theoretical_and_practical():
    assert rep.eoq(10000, 100, 10, 0.2) == pytest.approx(1000.0)          # sqrt(2*10000*100/(10*0.2))
    pq = rep.practical_qty(1000, moq=1200, multiple=500)
    assert pq["qty"] == 1500 and pq["theoretical"] == 1000 and pq["notes"]
    assert rep.eoq(0, 100, 10, 0.2) == 0


def test_practical_qty_reports_infeasible_cap():
    pq = rep.practical_qty(900, moq=1000, multiple=100, max_qty=800)
    assert pq["qty"] == 0 and pq["violations"]


# ---- replenishment policies -----------------------------------------------------------------------------------------------
BASE = dict(position=300, d=10, L=5, R=7, ss=40, rop=90, eoq=200)


def test_min_max_and_rop_policies():
    r = rep.recommend("MIN_MAX", {"min": 400, "max": 900}, BASE)
    assert r["triggered"] and r["raw_qty"] == 600
    r = rep.recommend("ROP", {}, {**BASE, "position": 50})
    assert r["triggered"] and r["raw_qty"] == 200          # one EOQ lifts position (250) above ROP 90
    assert not rep.recommend("ROP", {}, BASE)["triggered"]  # 300 > ROP


def test_order_up_to_base_stock_and_periodic():
    assert rep.recommend("ORDER_UP_TO", {"s": 350, "order_up_to": 800}, BASE)["raw_qty"] == 500
    assert rep.recommend("BASE_STOCK", {}, BASE)["order_up_to"] == pytest.approx(10 * 5 + 40)
    pr = rep.recommend("PERIODIC", {}, {**BASE, "position": 100})
    assert pr["order_up_to"] == pytest.approx(10 * 12 + 40) and pr["raw_qty"] == pytest.approx(60)


def test_kanban_jit_and_all_twelve_policies_evaluate():
    for name in rep.POLICIES:
        out = rep.recommend(name, {}, {**BASE, "planned": [], "sequenced_req": 100, "usable_plus_inbound_in_horizon": 50})
        assert out["policy"] == name and out["raw_qty"] >= 0
    k = rep.recommend("KANBAN", {"container_qty": 50, "cards": 4}, {**BASE, "position": 90})
    assert k["triggered"] and k["raw_qty"] % 50 == 0
    assert rep.recommend("JIT", {"window_days": 2}, {**BASE, "position": 20})["raw_qty"] == pytest.approx(10 * 7 - 20)


def test_mrp_plan_offsets_by_lead_time_and_flags_late_orders():
    plan = rep.mrp_plan(opening=50, receipts_daily=[0] * 30, demand_daily=[10] * 30, ss=0, L=5)
    assert plan and plan[0]["shortfall_day"] == 5 and plan[0]["release_day"] == 0 and not plan[0]["late"]
    late = rep.mrp_plan(opening=10, receipts_daily=[0] * 20, demand_daily=[10] * 20, ss=0, L=5)
    assert late[0]["late"]


# ---- ABC / XYZ / FSN / HML ---------------------------------------------------------------------------------------------------
def test_abc_uses_annual_consumption_value_and_configurable_thresholds():
    rows = [{"key": "A1", "annual_demand": 1000, "unit_cost": 100}, {"key": "B1", "annual_demand": 500, "unit_cost": 20},
            {"key": "C1", "annual_demand": 100, "unit_cost": 10}, {"key": "C2", "annual_demand": 50, "unit_cost": 10}]
    out = {r["key"]: r for r in seg.abc_classify(rows, {"A": 0.80, "B": 0.95})}
    assert out["A1"]["annual_value"] == 100000 and out["A1"]["abc"] == "A"
    assert out["A1"]["cum_pct"] == pytest.approx(100000 / 111500)
    strict = {r["key"]: r["abc"] for r in seg.abc_classify(rows, {"A": 0.5, "B": 0.6})}
    assert strict["C1"] == "C" and strict["B1"] == "C"


def test_xyz_by_coefficient_of_variation():
    rows = seg.xyz_classify([{"key": "x", "series": [100, 102, 98, 101]}, {"key": "y", "series": [50, 150, 60, 140]},
                             {"key": "z", "series": [0, 400, 0, 0]}], {"X": 0.5, "Y": 1.0})
    m = {r["key"]: r["xyz"] for r in rows}
    assert m == {"x": "X", "y": "X" if cv([50, 150, 60, 140]) <= 0.5 else "Y", "z": "Z"}
    assert m["z"] == "Z"


def test_fsn_hml_and_matrix():
    assert seg.fsn_classify(8, 10) == "F" and seg.fsn_classify(2, 30) == "S" and seg.fsn_classify(8, 400) == "N"
    h = seg.hml_classify({"a": 100, "b": 50, "c": 20, "d": 10, "e": 1})
    assert h["a"] == "H" and h["e"] == "L"
    m = seg.matrix([{"abc": "A", "xyz": "X", "annual_value": 5}, {"abc": "A", "xyz": "X", "annual_value": 7}])
    assert m["A"]["X"] == {"count": 2, "value": 12}


# ---- lead time statistics ---------------------------------------------------------------------------------------------------
def test_lead_time_stats_and_planning_basis():
    obs = [10, 12, 11, 14, 13, 20, 12, 11, 15, 25]
    st = lts.compute_stats(obs, static=10, min_obs=8)
    assert st.n == 10 and st.eligible and st.p50 == pytest.approx(12.5) and st.p90 > st.p50 > 0 and st.dist["type"] == "lognormal"
    L, s, _ = lts.planning_lead_time(st, "observed_mean", True, 7)
    assert L == pytest.approx(sum(obs) / 10) and s > 0
    L90, s90, _ = lts.planning_lead_time(st, "p90", True, 7)
    assert L90 == pytest.approx(st.p90) and s90 == 0               # variability already in the percentile
    Ls, _, note = lts.planning_lead_time(st, "observed_mean", False, 7)
    assert Ls == 10 and "static" in note                            # observed data can be switched off


def test_lead_time_not_eligible_falls_back_to_static():
    st = lts.compute_stats([9, 11], static=10, min_obs=8)
    assert not st.eligible
    assert lts.planning_lead_time(st, "p90", True, 7)[0] == 10


# ---- expiry / FEFO ------------------------------------------------------------------------------------------------------------
from datetime import date, timedelta
TODAY = date(2026, 9, 22)
TH = {"near_days": 60, "critical_days": 30, "near_pct": 0.35}


def test_expiry_status_and_shelf_life_pct():
    info = exp.expiry_info(TODAY + timedelta(days=45), TODAY - timedelta(days=55), None, TODAY, TH)
    assert info["days_to_expiry"] == 45 and info["pct_remaining"] == pytest.approx(0.45) and info["status"] == "NEAR"
    assert exp.expiry_info(TODAY + timedelta(days=10), None, 100, TODAY, TH)["status"] == "CRITICAL"
    assert exp.expiry_info(TODAY - timedelta(days=1), None, 100, TODAY, TH)["status"] == "EXPIRED"
    assert exp.expiry_info(None, None, None, TODAY, TH)["status"] == "NONE"


def test_fefo_picks_earliest_expiry_and_skips_ineligible_lots():
    lots = [{"lot_no": "L3", "qty": 100, "expiry": TODAY + timedelta(days=200), "state": "UNRESTRICTED", "quality": "RELEASED"},
            {"lot_no": "L1", "qty": 60, "expiry": TODAY + timedelta(days=40), "state": "UNRESTRICTED", "quality": "RELEASED"},
            {"lot_no": "LQ", "qty": 500, "expiry": TODAY + timedelta(days=20), "state": "QUARANTINED", "quality": "QUARANTINE"},
            {"lot_no": "LX", "qty": 500, "expiry": TODAY - timedelta(days=2), "state": "UNRESTRICTED", "quality": "RELEASED"}]
    plan = exp.fefo_pick(lots, 100, TODAY)
    assert [p["lot_no"] for p in plan["picks"]] == ["L1", "L3"] and plan["picks"][0]["qty"] == 60 and plan["picks"][1]["qty"] == 40
    assert {s["lot_no"] for s in plan["skipped"]} == {"LQ", "LX"} and plan["short"] == 0


def test_expiry_at_risk_uses_fefo_consumption_capacity():
    lots = [{"lot_no": "old", "qty": 300, "expiry": TODAY + timedelta(days=10)}, {"lot_no": "new", "qty": 300, "expiry": TODAY + timedelta(days=100)}]
    r = exp.expiry_at_risk(lots, daily_demand=10, today=TODAY)
    assert r[0]["at_risk_qty"] == 200 and r[1]["at_risk_qty"] == 0


def test_custom_aging_buckets():
    b = exp.aging_buckets([30, 60, 90, 180, 365])
    assert [x[0] for x in b] == ["0–30", "31–60", "61–90", "91–180", "181–365", "365+"]
    assert exp.bucket_for(45, b) == "31–60" and exp.bucket_for(1000, b) == "365+"
    assert [x[0] for x in exp.aging_buckets([15, 45])] == ["0–15", "16–45", "45+"]


# ---- projection / stock-out ---------------------------------------------------------------------------------------------------
def test_time_phased_projection_and_forecast_consumption():
    pr = proj.project(opening=100, forecast_daily=[10] * 28, order_demand=[{"qty": 35, "due_day": 3}], requirements=[], inbound=[{"qty": 100, "eta_day": 14, "promised_day": 14}],
                      transfers_out=[], safety_stock=20, horizon_days=28, step_days=7)
    w1 = pr["rows"][0]
    assert w1["order_demand"] == 35 and w1["forecast_demand"] == pytest.approx(35)      # 70 forecast − 35 already in orders → 35 left
    assert w1["ending"] == pytest.approx(100 - 70)
    assert pr["first_stockout_day"] is not None and pr["expected_shortage"] > 0
    assert pr["rows"][2]["receipts"] == 100


def test_stockout_probability_behaviour():
    hi = proj.stockout_probability(usable=50, inbound=[], demand_mean_T=100, demand_std_T=20, T=10)
    lo = proj.stockout_probability(usable=300, inbound=[], demand_mean_T=100, demand_std_T=20, T=10)
    assert hi["probability"] > 0.99 and lo["probability"] < 0.001
    late = proj.stockout_probability(usable=50, inbound=[{"qty": 100, "eta_day": 9, "sigma_days": 3}], demand_mean_T=100, demand_std_T=20, T=10)
    sure = proj.stockout_probability(usable=50, inbound=[{"qty": 100, "eta_day": 2, "sigma_days": 0}], demand_mean_T=100, demand_std_T=20, T=10)
    assert sure["probability"] < late["probability"] < hi["probability"]      # ETA uncertainty matters
    assert 0 < late["expected_shortage"] < hi["expected_shortage"]


def test_risk_levels_are_configurable_and_time_escalated():
    th = {"medium": 0.05, "high": 0.15, "critical": 0.35}
    assert stockout_level(0.02, None, 10, th) == "LOW" and stockout_level(0.10, None, 10, th) == "MEDIUM" and stockout_level(0.5, None, 10, th) == "CRITICAL"
    assert stockout_level(0.10, 5, 10, th) == "HIGH"                       # shortfall inside the lead time escalates one band
    assert stockout_level(0.10, None, 10, {"medium": 0.01, "high": 0.05, "critical": 0.09}) == "CRITICAL"


# ---- UOM ------------------------------------------------------------------------------------------------------------------------
def test_uom_conversion_and_validation():
    assert uom.convert(1500, "G", "KG") == pytest.approx(1.5)
    assert uom.convert(2, "PALLET", "EA", {("PALLET", "CASE"): 40, ("CASE", "EA"): 24}) == 1920
    assert uom.convert(48, "EA", "CASE", {("CASE", "EA"): 24}) == 2
    for bad in (("KG", "LITRE", None), ("CASE", "EA", None), ("EA", "FURLONG", None)):
        with pytest.raises(uom.UomConversionError):
            uom.convert(1, bad[0], bad[1], bad[2])


# ---- rules / policies -----------------------------------------------------------------------------------------------------------
def test_rule_engine_compares_metrics_to_metrics_and_literals():
    cond = {"all": [{"metric": "projected_min", "op": "<", "value": "safety_stock"}, {"metric": "demand", "op": ">", "value": 0}]}
    ok, ev = eval_condition(cond, {"projected_min": 10, "safety_stock": 50, "demand": 3})
    assert ok and len(ev) == 2
    assert not eval_condition(cond, {"projected_min": 100, "safety_stock": 50, "demand": 3})[0]
    assert not eval_condition(cond, {"projected_min": None, "safety_stock": 50, "demand": 3})[0]      # missing data never fires
    assert eval_condition({"any": [{"metric": "s", "op": "in", "value": ["A", "B"]}]}, {"s": "B"})[0]
    with pytest.raises(ValueError):
        eval_condition({"metric": "x", "op": "exec", "value": 1}, {"x": 1})
    assert severity_for({"default": "MEDIUM", "when": [{"metric": "n", "op": ">", "value": 5, "severity": "CRITICAL"}]}, {"n": 9}) == "CRITICAL"


def test_safe_formula_evaluator_blocks_code_execution():
    assert safe_eval("365 * inv / cogs", {"inv": 10, "cogs": 100}) == pytest.approx(36.5)
    for evil in ('__import__("os").system("x")', "open('/etc/passwd')", "().__class__", "[x for x in y]", "a.b"):
        with pytest.raises(FormulaError):
            safe_eval(evil, {"a": 1, "y": 1})
    with pytest.raises(FormulaError):
        safe_eval("missing + 1", {})


def test_kpi_status_directions():
    assert kpi_status(0.97, "higher", 0.95, 0.9, 0.8) == "NORMAL" and kpi_status(0.92, "higher", 0.95, 0.9, 0.8) == "WATCH"
    assert kpi_status(0.85, "higher", 0.95, 0.9, 0.8) == "ATTENTION" and kpi_status(0.7, "higher", 0.95, 0.9, 0.8) == "CRITICAL"
    assert kpi_status(80, "lower", 60, 90, 150) == "WATCH" and kpi_status(200, "lower", 60, 90, 150) == "CRITICAL"


class _P:
    def __init__(self, id, level, key, params):
        self.id, self.scope_level, self.scope_key, self.params, self.active = id, level, key, params, True


def test_policy_hierarchy_more_specific_overrides_broader_and_merges():
    item, loc = {"sku": "S1", "category": "Cat", "industry": "AUTO"}, {"code": "N1", "region": "West", "industry": "AUTO"}
    ps = [_P(1, "GLOBAL", "*", {"service_level": 0.95, "method": "combined"}), _P(2, "INDUSTRY", "AUTO", {"service_level": 0.99}), _P(3, "NODE", "N1", {"review_days": 3}),
          _P(4, "SKU", "S1", {"service_level": 0.97}), _P(5, "SKU_LOCATION", "S1|N1", {"service_level": 0.995}), _P(6, "SKU", "OTHER", {"service_level": 0.5})]
    merged, trail = pol.resolve(ps, item, loc)
    assert merged == {"service_level": 0.995, "method": "combined", "review_days": 3}
    assert [t["level"] for t in trail] == ["GLOBAL", "INDUSTRY", "NODE", "SKU", "SKU_LOCATION"]
    assert pol.PolicyIndex(ps).resolve(item, loc)[0] == merged
