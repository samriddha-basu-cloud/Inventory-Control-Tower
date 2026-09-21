"""
Multi-Echelon Inventory Optimization (MEIO) — section 38-39.

METHODOLOGY (documented, not hidden):
This module implements a *risk-pooling MEIO approximation* for a two-tier
network (one upstream node, e.g. a central DC, feeding N downstream nodes,
e.g. regional DCs/stores). It is NOT a full guaranteed-service or
stochastic-service multi-echelon solve (that requires a nonlinear
network solver such as OR-Tools/PuLP over a DAG with service-time
variables) — it is a defensible, explainable approximation commonly used
for a first-pass network safety-stock allocation:

1. Each downstream node keeps *local* safety stock sized only for the
   lead time from the upstream node to itself (short internal replenishment
   lead time), not the full external supplier lead time.
2. The upstream (central) node absorbs the *external* supplier lead-time
   uncertainty and pools demand variability across all the nodes it feeds,
   using the square-root-of-time / independent-variance pooling law:
       sigma_pooled = sqrt( sum(sigma_i^2) )
   instead of summing each node's sigma independently. This captures the
   real statistical benefit of holding safety stock further upstream
   (risk pooling) and is why MEIO safety stock is typically lower than the
   sum of independently-optimized single-echelon safety stocks.

Do not present this as a universal MEIO algorithm — it assumes demand
across downstream nodes is independent (no strong positive correlation)
and a simple two-tier topology.
"""
import math
from app.optimization.safety_stock import combined_variability


def optimize_two_echelon(upstream, downstream_nodes):
    """
    upstream: dict(node, external_lead_time_days, external_std_dev_lead_time_days,
                    service_level_pct, unit_cost)
    downstream_nodes: list of dict(node, avg_demand_daily, std_dev_demand_daily,
                    internal_lead_time_days, internal_std_dev_lead_time_days,
                    service_level_pct, unit_cost)
    """
    # --- Downstream: local safety stock only covers internal replenishment lead time
    downstream_results = []
    pooled_variance = 0.0
    pooled_avg_demand = 0.0
    for n in downstream_nodes:
        calc = combined_variability(
            n["avg_demand_daily"], n["std_dev_demand_daily"],
            n.get("internal_lead_time_days", 1.0), n.get("internal_std_dev_lead_time_days", 0.0),
            n["service_level_pct"],
        )
        ss = calc["safety_stock"]
        downstream_results.append({
            **n, "safety_stock": ss,
            "safety_stock_value": round(ss * n.get("unit_cost", 0.0), 2),
            "calc": calc,
        })
        pooled_variance += n["std_dev_demand_daily"] ** 2
        pooled_avg_demand += n["avg_demand_daily"]

    pooled_std_dev = math.sqrt(pooled_variance)

    # --- Upstream: pooled demand variability + full external lead-time uncertainty
    upstream_calc = combined_variability(
        pooled_avg_demand, pooled_std_dev,
        upstream["external_lead_time_days"], upstream.get("external_std_dev_lead_time_days", 0.0),
        upstream["service_level_pct"],
    )
    upstream_ss = upstream_calc["safety_stock"]

    total_downstream_ss = sum(d["safety_stock"] for d in downstream_results)
    total_ss_units = total_downstream_ss + upstream_ss
    total_ss_value = sum(d["safety_stock_value"] for d in downstream_results) + \
        upstream_ss * upstream.get("unit_cost", 0.0)

    return {
        "mode": "multi_echelon_two_tier",
        "methodology": "risk_pooling_approximation",
        "upstream": {**upstream, "safety_stock": upstream_ss,
                     "safety_stock_value": round(upstream_ss * upstream.get("unit_cost", 0.0), 2),
                     "pooled_std_dev_demand": round(pooled_std_dev, 2),
                     "calc": upstream_calc},
        "downstream": downstream_results,
        "total_safety_stock_units": round(total_ss_units, 2),
        "total_safety_stock_value": round(total_ss_value, 2),
    }


def compare_single_vs_multi_echelon(single_echelon_result, multi_echelon_result):
    """
    IMPORTANT — this is a genuine trade-off, not a one-directional win:
    pooling demand at the hub reduces safety stock driven by *idiosyncratic
    demand variability* (independent noise across nodes averages out), but
    it concentrates safety stock driven by *shared lead-time variability*
    (the whole aggregated order now rides on one uncertain supplier lead
    time instead of many independent draws). Which effect dominates depends
    on the ratio of demand CV to lead-time CV in the network. The result
    below reports whichever direction the numbers actually show - it is
    never forced positive.
    """
    se_units = single_echelon_result["total_safety_stock_units"]
    me_units = multi_echelon_result["total_safety_stock_units"]
    se_value = single_echelon_result["total_safety_stock_value"]
    me_value = multi_echelon_result["total_safety_stock_value"]
    reduction_units = se_units - me_units
    reduction_pct = (reduction_units / se_units * 100.0) if se_units else 0.0
    release = se_value - me_value

    if release > 0:
        verdict = ("Demand-variability pooling outweighs shared lead-time risk here - "
                   "centralizing safety stock at the hub releases working capital.")
    elif release < 0:
        verdict = ("Shared external lead-time variability outweighs the demand-pooling benefit here - "
                   "centralizing safety stock at the hub would actually require MORE network safety stock. "
                   "Consider reducing supplier lead-time variability (dual-sourcing, tighter SLAs) before "
                   "centralizing, or keep buffers closer to demand for this SKU.")
    else:
        verdict = "No material difference between single- and multi-echelon safety stock for this SKU."

    return {
        "single_echelon_safety_stock_units": se_units,
        "multi_echelon_safety_stock_units": me_units,
        "safety_stock_reduction_units": round(reduction_units, 2),
        "safety_stock_reduction_pct": round(reduction_pct, 1),
        "single_echelon_safety_stock_value": se_value,
        "multi_echelon_safety_stock_value": me_value,
        "working_capital_release": round(release, 2),
        "verdict": verdict,
    }
