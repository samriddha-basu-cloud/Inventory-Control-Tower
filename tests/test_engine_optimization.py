"""Engine invariants on demo data, allocation / pegging, optimisation constraints, stock-out & excess detection."""
import copy

import pytest

from app.models import Allocation, InventoryBalance, Peg
from app.optimization import replenishment as repopt
from app.optimization import transfer as trfopt
from app.optimization.allocation import solve_allocation
from app.services import allocation_service as alloc
from app.services import optimization_service as opt
from app.services import settings_service as S
from app.services import simulation_service as sim
from app.services.engine import compute_pair
from app.services.snapshot import PairFilter, get_snapshot


def test_snapshot_invariants_hold_for_every_item_location(ctx):
    snap = get_snapshot(force=True)
    assert len(snap.results) > 100
    for k, r in snap.results.items():
        p = r.pos
        assert p["available"] == pytest.approx(p["usable_on_hand"] - p["allocated"] - p["committed"] - p["reserved"])
        assert p["position"] == pytest.approx(p["usable_on_hand"] + p["in_transit"] + p["on_order"] - p["allocated"] - p["committed"] - p["reserved"] - p["backorder"])
        assert p["on_hand"] >= p["usable_on_hand"] - 1e-9                       # usable stock is a subset of physical
        assert r.ss >= 0 and r.rop >= r.ss - 1e-9 and 0 <= r.stockout_prob <= 1 and 0 <= r.confidence <= 1
        assert r.excess_qty >= 0 and r.excess_value >= 0 and r.rec["qty"] >= 0


def test_stockout_and_excess_are_detected_where_they_were_planted(ctx):
    snap = get_snapshot()
    by = {(i.sku, i.loc_code): r for i, r in snap.rows()}
    assert by[("FMC-BEV-COLA-1L", "FMC-RDC-S")].risk_level in ("MEDIUM", "HIGH", "CRITICAL")           # shortage story
    assert by[("FMC-BEV-COLA-1L", "FMC-RDC-W")].excess_qty > 0 and by[("FMC-BEV-COLA-1L", "FMC-RDC-W")].inv_class in ("EXCESS", "SLOW")
    assert by[("PHM-TAB-LEGACY", "PHM-CDC-01")].inv_class == "OBSOLETE" and by[("PHM-TAB-LEGACY", "PHM-CDC-01")].obsolete_value > 0


def test_expiry_and_quality_states_from_demo(ctx):
    snap = get_snapshot()
    by = {(i.sku, i.loc_code): r for i, r in snap.rows()}
    assert by[("PHM-INJ-VACC", "PHM-RDC-E")].expiry_status in ("EXPIRED", "CRITICAL") and by[("PHM-INJ-VACC", "PHM-RDC-E")].expired_qty > 0
    insulin = snap.inputs[next(k for k, i in snap.inputs.items() if i.sku == "PHM-INJ-INSUL" and i.loc_code == "PHM-CDC-01")]
    quarantined = sum(l["qty"] for l in insulin.lots if l["state"] == "QUARANTINED")
    assert quarantined > 0 and snap.results[insulin.key].pos["usable_on_hand"] <= snap.results[insulin.key].pos["on_hand"] - quarantined + 1e-6


def test_observed_lead_time_materially_changes_risk_when_enabled(ctx):
    snap = get_snapshot()
    inp = next(i for i in snap.inputs.values() if i.lt_obs and i.static_lt and len(i.lt_obs) >= 8 and i.supplier_id and snap.results[i.key].d_mean > 0)
    cfg_on = copy.deepcopy(snap.cfg)
    cfg_on.use_observed_lt, cfg_on.lt_basis = True, "p90"
    cfg_off = copy.deepcopy(snap.cfg)
    cfg_off.use_observed_lt = False
    on, off = compute_pair(inp, cfg_on), compute_pair(inp, cfg_off)
    assert on.lt_plan != off.lt_plan and off.lt_plan == pytest.approx(inp.static_lt)
    assert on.lt_p90 is not None and on.lt_p90 >= (on.lt_stats["p50"] or 0)
    assert (on.ss, on.rop) != (off.ss, off.rop)


def test_replenishment_recommendation_respects_moq_and_multiple(ctx):
    snap = get_snapshot()
    n = 0
    for k, r in snap.results.items():
        inp = snap.inputs[k]
        q = r.rec["qty"]
        if q > 0 and inp.moq and not r.rec["violations"]:
            n += 1
            assert q >= inp.moq - 1e-6
            if inp.multiple:
                assert (q / inp.multiple) == pytest.approx(round(q / inp.multiple), abs=1e-6)
            assert r.rec["post_order_position"] == pytest.approx(r.pos["position"] + q)
    assert n > 0


# ---- allocation & pegging -----------------------------------------------------------------------------------------------------
def test_pegging_never_double_commits_or_over_allocates(ctx):
    snap = get_snapshot(force=True)
    alloc.run_pegging_all()
    pegs = Peg.query.all()
    assert pegs
    per_demand, per_supply = {}, {}
    for p in pegs:
        per_demand[(p.item_id, p.location_id, p.demand_ref)] = per_demand.get((p.item_id, p.location_id, p.demand_ref), 0) + p.qty
        per_supply[(p.item_id, p.location_id, p.supply_type, p.supply_ref)] = per_supply.get((p.item_id, p.location_id, p.supply_type, p.supply_ref), 0) + p.qty
    for (iid, lid, ref), q in per_demand.items():
        inp = snap.inputs[(iid, lid)]
        demand_qty = sum(o["qty"] for o in inp.orders + inp.extra.get("backorders", []) if o["ref"] == ref) + sum(r["qty"] for r in inp.requirements if r["ref"] == ref)
        assert q <= demand_qty + 1e-6                                     # never more than the demand asked for
    for (iid, lid, typ, ref), q in per_supply.items():
        inp = snap.inputs[(iid, lid)]
        if typ == "ONHAND":
            assert q <= sum(l["qty"] for l in inp.lots if l["state"] == "UNRESTRICTED" and (l.get("lot_no") or "STOCK") == ref) + 1e-6
        else:
            assert q <= sum(i["qty"] for i in inp.inbound if i["ref"] == ref) + 1e-6      # never more than the supply has


def test_peg_only_released_unexpired_stock_for_pharma(ctx):
    snap = get_snapshot()
    for a in Allocation.query.filter_by(status="ACTIVE").all():
        if a.lot_id:
            inp = snap.inputs[(a.item_id, a.location_id)]
            lot = next(l for l in inp.lots if l["lot_id"] == a.lot_id and l["state"] == "UNRESTRICTED")
            assert lot["quality"] == "RELEASED" and (lot["expiry"] is None or lot["expiry"] >= snap.today)


def test_manual_allocation_refuses_over_allocation(ctx):
    snap = get_snapshot(force=True)
    k = next(k for k, r in snap.results.items() if r.pos["available"] > 10)
    avail = snap.results[k].pos["available"]
    with pytest.raises(alloc.OverAllocationError):
        alloc.commit_allocation(k[0], k[1], avail + 1)


def test_allocation_policies_rank_differently():
    item, loc = {"selling_price": 100, "unit_cost": 60, "criticality": "High"}, {"region": "West"}
    custs = {1: {"priority": 1, "region": "West", "contract_priority": True}, 2: {"priority": 4, "region": "East", "contract_priority": False}}
    ds = [{"ref": "SO-A", "qty": 10, "due_day": 5, "customer_id": 2, "customer_priority": 4, "value": 5000, "order_date": "2026-01-01"},
          {"ref": "SO-B", "qty": 10, "due_day": 5, "customer_id": 1, "customer_priority": 1, "value": 800, "order_date": "2026-02-01"}]
    assert alloc.rank_demands(ds, item, loc, {"method": "fifo"}, custs)[0]["ref"] == "SO-A"           # oldest order first
    assert alloc.rank_demands(ds, item, loc, {"method": "priority"}, custs)[0]["ref"] == "SO-B"       # priority customer
    assert alloc.rank_demands(ds, item, loc, {"method": "revenue"}, custs)[0]["ref"] == "SO-A"        # bigger revenue
    assert alloc.rank_demands(ds, item, loc, {"method": "contract"}, custs)[0]["ref"] == "SO-B"


def test_allocation_lp_respects_supply_demand_and_min_fill():
    r = solve_allocation(100, [{"id": "a", "qty": 80, "score": 1.0}, {"id": "b", "qty": 80, "score": 0.1}])
    assert r["status"] == "OPTIMAL" and sum(r["allocations"].values()) == pytest.approx(100) and r["allocations"]["a"] == pytest.approx(80)
    fair = solve_allocation(100, [{"id": "a", "qty": 80, "score": 1.0}, {"id": "b", "qty": 80, "score": 0.1}], min_fill=0.8)
    assert fair["allocations"]["b"] >= 0.8 * 50 - 1e-6
    assert all(v <= 80 + 1e-6 for v in fair["allocations"].values())
    bad = solve_allocation(10, [{"id": "a", "qty": 80, "score": 1}, {"id": "b", "qty": 80, "score": 1}], min_fill=5.0)
    assert bad["status"] in ("OPTIMAL", "INFEASIBLE")


# ---- optimisation constraints -----------------------------------------------------------------------------------------------------
SKUS = [{"id": "S1@N", "need": 700, "moq": 500, "mult": 100, "max_qty": 5000, "unit_cost": 100, "weight_kg": 1, "volume_m3": 0.01, "supplier": 1, "holding_unit": 2, "short_cost": 60,
         "expedite_unit": 30, "obsolescence_unit": 3, "co2_unit": 0.1, "shelf_cap": None},
        {"id": "S2@N", "need": 300, "moq": 400, "mult": 50, "max_qty": 5000, "unit_cost": 50, "weight_kg": 1, "volume_m3": 0.01, "supplier": 1, "holding_unit": 1, "short_cost": 30,
         "expedite_unit": 15, "obsolescence_unit": 2, "co2_unit": 0.1, "shelf_cap": None}]
W = {"stockout": 5, "expedite": 1, "holding": 1, "working_capital": 1, "obsolescence": 1, "carbon": 1}


def test_milp_honours_moq_multiples_budget_and_supplier_capacity():
    res = repopt.solve(SKUS, [{"id": 1, "capacity": 2000, "truck_cost": 1000}], {"budget": 200000, "truck_kg": 5000, "service_floor": 0.9}, W, enforce_service=True)
    assert res["status"] == "OPTIMAL"
    for p, s in zip(res["plan"], SKUS):
        assert p["qty"] == 0 or (p["qty"] >= s["moq"] and p["qty"] % s["mult"] == 0)
        assert p["qty"] + p["expedited_units"] >= 0.9 * s["need"] - 1e-6            # service floor
    assert sum(p["spend"] for p in res["plan"]) <= 200000 + 1e-6 and sum(p["qty"] for p in res["plan"]) <= 2000 + 1e-6
    assert all(p["expedited_units"] <= 0.3 * p["need"] + 1e-6 for p in res["plan"])           # recourse is capped
    assert {o["term"] for o in res["objective"]} >= {"stockout", "holding", "obsolescence", "carbon"} and all("weight" in o for o in res["objective"])


def test_milp_infeasibility_is_explained_not_hidden():
    res = repopt.solve(SKUS, [{"id": 1, "capacity": 2000, "truck_cost": 1000}], {"budget": 1000, "truck_kg": 5000, "service_floor": 0.95}, W, enforce_service=True)
    assert res["status"] == "INFEASIBLE" and res["plan"] == []
    text = " ".join(res["explanation"]).lower()
    assert "budget" in text and "cheapest plan" in text


def test_objective_weights_change_the_plan():
    cons = {"budget": 500000, "truck_kg": 10000, "service_floor": 0.0}
    sup = [{"id": 1, "capacity": None, "truck_cost": 0}]
    cheap_stock = repopt.solve(SKUS, sup, cons, {**W, "stockout": 0.001, "expedite": 1000}, enforce_service=False)
    care_stock = repopt.solve(SKUS, sup, cons, {**W, "stockout": 100}, enforce_service=False)
    assert sum(p["qty"] for p in care_stock["plan"]) > sum(p["qty"] for p in cheap_stock["plan"])


def test_transfer_lp_blocks_impossible_lanes_and_explains_unmet_need():
    donors, receivers = [{"id": "D1", "qty": 500}], [{"id": "R1", "need": 300, "penalty": 100}, {"id": "R2", "need": 300, "penalty": 100}]
    arcs = {("D1", "R1"): {"cost": 2, "cap": float("inf"), "blocked": None, "days": 1}, ("D1", "R2"): {"cost": 1, "cap": float("inf"), "blocked": "arrives after projected stock-out", "days": 9}}
    res = trfopt.solve_transfers(donors, receivers, arcs)
    assert sum(f["qty"] for f in res["flows"] if f["receiver"] == "R1") == 300 and not any(f["receiver"] == "R2" for f in res["flows"])
    assert res["unmet"]["R2"] == 300 and any("stock-out" in e for e in res["explanation"])
    assert trfopt.solve_transfers(donors, receivers, arcs, require_full=True)["status"] == "INFEASIBLE"


def test_donors_never_go_below_safety_stock(ctx):
    snap = get_snapshot()
    plan = opt.rebalancing_plan(snap)
    assert plan["transfers"]
    for t in plan["transfers"]:
        donor = snap.results[(t["item_id"], t["from_id"])]
        assert donor.pos["available"] - t["qty"] >= donor.ss - 1e-6
        assert t["after"]["to_position"] > t["before"]["to_position"] and t["after"]["to_prob"] <= t["before"]["to_prob"] + 1e-9


def test_replenishment_plan_on_real_data_respects_budget(ctx):
    snap = get_snapshot()
    res = opt.replenishment_plan(snap, budget=2_000_000, truck_kg=12000)
    assert res["status"] in ("OPTIMAL", "INFEASIBLE")
    if res["status"] == "OPTIMAL":
        assert sum(p["spend"] for p in res["plan"]) <= 2_000_000 + 1e-3
    else:
        assert res["explanation"]


def test_decision_options_are_explained_and_infeasible_ones_flagged(ctx):
    snap = get_snapshot()
    k = next(k for k, r in snap.results.items() if r.risk_level in ("HIGH", "CRITICAL", "MEDIUM") and r.d_mean > 0)
    res = opt.generate_options(snap, k)
    assert any(o["type"] == "DO_NOTHING" for o in res["options"])
    for o in res["options"]:
        assert {"stockout_prob", "exp_short", "risk_level"} <= set(o["after"]) and o["feasible"] == (not o["violations"])
    if res["recommended"]:
        assert res["recommended"]["feasible"]
    else:
        assert res["note"]
    ex = opt.decision_explainer(snap.inputs[k], snap.results[k])
    assert ex["headline"] and len(ex["rows"]) >= 8


# ---- simulation / digital twin -----------------------------------------------------------------------------------------------------
def _checksum():
    return round(sum(b.quantity for b in InventoryBalance.query.all()), 6)


def test_scenario_never_modifies_production_data_and_is_governed(ctx):
    from app.extensions import db
    before = _checksum()
    row = sim.run_scenario("port delay", [{"type": "port_delay", "params": {"port": "Nhava Sheva (JNPT)", "delay_days": 12, "start_day": 0, "duration_days": 45}}], runs=12, created_by="tester")
    db.session.commit()
    assert _checksum() == before
    assert row.scenario_no and row.created_by == "tester" and row.base_dataset and row.changes and row.results and row.created_at
    d = {x["metric"]: x for x in row.results["deltas"]}
    assert d["revenue_risk"]["delta"] >= 0 and d["service_level"]["delta"] <= 1e-9         # a port delay cannot improve service


def test_scenarios_move_metrics_in_the_expected_direction(ctx):
    snap = get_snapshot()
    base = sim.simulate(snap, [], 91, 7, 15, seed=3)["metrics"]
    demand_up = sim.simulate(snap, [{"type": "demand_increase", "params": {"pct": 50, "scope": "all", "start_day": 0, "duration_days": 60}}], 91, 7, 15, seed=3)["metrics"]
    assert demand_up["service_level"] <= base["service_level"] + 1e-9 and demand_up["revenue_risk"] >= base["revenue_risk"]
    more_ss = sim.simulate(snap, [{"type": "safety_stock_increase", "params": {"pct": 50, "scope": "all"}}], 91, 7, 15, seed=3)["metrics"]
    assert more_ss["service_level"] >= base["service_level"] - 1e-9 and more_ss["carrying_cost"] >= base["carrying_cost"] - 1e-6
    shutdown = sim.simulate(snap, [{"type": "supplier_shutdown", "params": {"supplier": "AUT-S-SHAN", "start_day": 0, "duration_days": 60}}], 91, 7, 15, seed=3)["metrics"]
    assert shutdown["revenue_risk"] >= base["revenue_risk"]
    expedite = sim.simulate(snap, [{"type": "emergency_purchase", "params": {"sku": "AUT-ECU-CTL", "location": "AUT-PLT-CHK", "qty": 1000, "lead_days": 2, "premium_pct": 25}}], 91, 7, 15, seed=3)["metrics"]
    assert expedite["expedite_cost"] > 0 and expedite["carbon_kg"] > base["carbon_kg"]


def test_simulation_supports_daily_weekly_monthly_steps_and_is_reproducible(ctx):
    snap = get_snapshot()
    keys = list(snap.inputs)[:25]
    a = sim.simulate(snap, [], 90, 1, 10, seed=5, pair_keys=keys)
    b = sim.simulate(snap, [], 90, 1, 10, seed=5, pair_keys=keys)
    assert a["metrics"] == b["metrics"]
    assert len(a["series"]["labels"]) == 90 and len(sim.simulate(snap, [], 91, 7, 5, pair_keys=keys)["series"]["labels"]) == 13 and len(sim.simulate(snap, [], 180, 30, 5, pair_keys=keys)["series"]["labels"]) == 6
