"""Model eligibility checks and policy experiments (A/B back-tests on the digital twin).

Eligibility answers *should we use this advanced model on this data?* with RECOMMENDED / POSSIBLE / NOT RECOMMENDED and
the evidence. Experiments run two policy arms through the same simulation (common random numbers) and report cost-of-
service trade-offs; results are stored, never applied to production.
"""
from __future__ import annotations

from ..extensions import db
from ..models import Action, Experiment, Recommendation
from . import audit_service as audit
from . import kpi_service
from . import simulation_service as sim
from .snapshot import Snapshot, get_snapshot

R, P, N = "RECOMMENDED", "POSSIBLE", "NOT RECOMMENDED"


def _grade(share: float, hi: float = 0.8, lo: float = 0.5) -> str:
    return R if share >= hi else (P if share >= lo else N)


def eligibility(snap: Snapshot) -> list[dict]:
    pairs = list(snap.inputs.values())
    res = [snap.results[k] for k in snap.results]
    n = max(len(pairs), 1)
    dem = [i for i in pairs if snap.results[i.key].d_mean > 0] or pairs
    nd = max(len(dem), 1)
    hist26 = sum(1 for i in dem if len(i.hist_weekly) >= 26) / nd
    smooth = sum(1 for i in dem if snap.results[i.key].demand_profile in ("SMOOTH", "ERRATIC")) / nd
    inter = sum(1 for i in dem if snap.results[i.key].demand_profile in ("INTERMITTENT", "LUMPY")) / nd
    lt_elig = sum(1 for r in res if r.lt_stats.get("eligible")) / n
    cost = sum(1 for i in pairs if i.item.get("unit_cost")) / n
    moq = sum(1 for i in pairs if i.moq) / n
    sourced = sum(1 for i in pairs if i.supplier_id or i.source_loc_id) / n
    fc_pairs = sum(1 for i in dem if len(i.fc_pairs) >= 8) / nd
    coords = sum(1 for l in snap.locs.values() if l.get("lat") is not None) / max(len(snap.locs), 1)
    parents = sum(1 for i in pairs if i.source_loc_id) / n
    fresh = snap.freshness["score"]
    out = [
        {"model": "Normal-approximation safety stock", "status": _grade(min(hist26, smooth + 0.2)), "evidence": f"{hist26:.0%} of demand-bearing SKU-locations have ≥ 26 weeks of history; {smooth:.0%} have smooth/erratic (non-intermittent) demand",
         "requires": "≥ 26 weeks history, non-intermittent demand", "fallback": "Empirical bootstrap safety stock is applied automatically to intermittent/lumpy items"},
        {"model": "Empirical bootstrap safety stock (intermittent demand)", "status": R if inter > 0.15 else (P if inter > 0 else N), "evidence": f"{inter:.0%} of SKU-locations classified intermittent/lumpy (Syntetos-Boylan)",
         "requires": "≥ 20 weeks of history for the resampling", "fallback": "Combined-variability formula"},
        {"model": "Probabilistic lead-time distribution (lognormal)", "status": _grade(lt_elig), "evidence": f"{lt_elig:.0%} of SKU-locations have ≥ min observations ({snap.cfg.min_lt_obs}) of lead time",
         "requires": f"≥ {snap.cfg.min_lt_obs} observed lead times per supplier-item", "fallback": "Static master lead time (no variability assumed)"},
        {"model": "Forecast-error based safety stock", "status": _grade(fc_pairs), "evidence": f"{fc_pairs:.0%} have ≥ 8 weeks of forecast-vs-actual pairs", "requires": "paired forecast/actual history", "fallback": "Demand-history σ"},
        {"model": "MILP replenishment optimisation", "status": _grade(min(cost, moq, sourced), 0.85, 0.6), "evidence": f"cost {cost:.0%}, MOQ {moq:.0%}, sourcing {sourced:.0%} complete",
         "requires": "unit cost, MOQ/multiples, supplier for the SKUs in scope", "fallback": "Policy-based recommendations (ROP/EOQ) without cross-SKU trade-offs"},
        {"model": "Network transfer / rebalancing optimisation", "status": _grade(min(parents, coords), 0.7, 0.4), "evidence": f"{parents:.0%} of pairs have a defined upstream node; {coords:.0%} of nodes have coordinates",
         "requires": "complete network, coordinates or lane costs", "fallback": "Manual transfer proposals"},
        {"model": "Monte-Carlo simulation / digital twin", "status": _grade(min(hist26, cost, fresh), 0.75, 0.5), "evidence": f"history {hist26:.0%}, cost {cost:.0%}, data freshness {fresh:.0%}",
         "requires": "demand σ, lead-time data, costs, fresh inputs", "fallback": "Deterministic projection only"},
        {"model": "Constrained allocation (LP)", "status": R if any(i.orders for i in pairs) else N, "evidence": f"{sum(1 for i in pairs if i.orders)} SKU-locations with open demand orders", "requires": "open orders with priorities",
         "fallback": "Sequential policy ranking"},
    ]
    return out


# ---------------------------------------------------------------------------------------------------------- experiments
EXPERIMENT_TYPES = {
    "service_level": "Service-level target A vs B", "safety_stock": "Safety-stock scale A vs B", "lead_time_buffer": "Lead-time buffer +%: A vs B",
    "moq": "MOQ scale A vs B",
}


def _arm_changes(kind: str, value: float, scope: str | None):
    if kind == "service_level":
        return [{"type": "service_level_change", "params": {"service_level": value, "scope": scope or "all"}}]
    if kind == "safety_stock":
        return [{"type": "safety_stock_increase", "params": {"pct": value, "scope": scope or "all"}}]
    if kind == "lead_time_buffer":
        return [{"type": "lead_time_increase", "params": {"pct": value, "scope": scope or "all"}}]
    if kind == "moq":
        return [{"type": "moq_increase", "params": {"pct": value, "scope": scope or "all"}}]
    raise ValueError(f"Unknown experiment type '{kind}'")


def run_experiment(name: str, kind: str, a: float, b: float, *, scope: str | None = None, hypothesis: str = "", horizon_days: int = 91, runs: int = 30,
                   seed: int = 7, created_by: str = "system") -> Experiment:
    snap = get_snapshot()
    keys = [k for k, inp in snap.inputs.items() if sim._scope_match(scope, inp, snap)]
    if not keys:
        raise ValueError("Scope matches no SKU-locations.")
    arms = {}
    for label, val in (("A", a), ("B", b)):
        res = sim.simulate(snap, _arm_changes(kind, val, scope), horizon_days, 7, runs, seed, pair_keys=keys)
        m = res["metrics"]
        total = m["carrying_cost"] + m["lost_margin"] + m["expedite_cost"]
        arms[label] = {"value": val, "metrics": m, "total_cost_proxy": total}
    winner = min(arms, key=lambda x: arms[x]["total_cost_proxy"])
    exp = Experiment(name=name, hypothesis=hypothesis, config={"type": kind, "a": a, "b": b, "scope": scope, "horizon_days": horizon_days, "runs": runs, "seed": seed, "pairs": len(keys)},
                     results={"arms": arms, "winner": winner, "objective": "carrying cost + lost margin + expedite cost over the horizon",
                              "note": "Back-test on the digital twin with common random numbers; not applied to production."}, status="DONE", created_by=created_by)
    db.session.add(exp)
    db.session.flush()
    exp.exp_no = f"EXP-{exp.id:05d}"
    audit.log("SCENARIO", "Experiment", exp.exp_no, "experiment_run", {"type": kind, "a": a, "b": b, "winner": winner}, actor=created_by)
    return exp


def learning_stats() -> dict:
    """LEARN stage: how often are recommendations acted on and what happens to actions?"""
    recs = Recommendation.query.all()
    acts = Action.query.all()
    by_status = {}
    for a in acts:
        by_status[a.status] = by_status.get(a.status, 0) + 1
    decided = by_status.get("VERIFIED", 0) + by_status.get("EXECUTED", 0) + by_status.get("REJECTED", 0)
    return {"recommendations": len(recs), "actioned": sum(1 for r in recs if r.status == "ACTIONED"), "dismissed": sum(1 for r in recs if r.status == "DISMISSED"),
            "actions": len(acts), "by_status": by_status, "acceptance_rate": ((by_status.get("VERIFIED", 0) + by_status.get("EXECUTED", 0)) / decided) if decided else None}
