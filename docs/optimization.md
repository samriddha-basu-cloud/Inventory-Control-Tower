# Optimization Methodology

## Single-echelon baseline

Every regional node computes its own safety stock as if it ordered directly
from the external supplier, independently of every other node:
```
SS_i = Z * sqrt(LT_ext * σ_Di² + D̄i² * σ_LT_ext²)
```
Network total = sum of every node's independent safety stock.
(`app/optimization/single_echelon.py`)

## Multi-echelon (two-tier risk-pooling approximation)

- Downstream nodes hold safety stock only for the **short internal
  replenishment lead time** from the hub (e.g. 2 days), not the full
  external supplier lead time.
- The hub pools downstream demand variability using independent-variance
  pooling: `σ_pooled = sqrt(Σ σ_Di²)` (smaller than `Σ σ_Di`), and absorbs
  the full external supplier lead-time uncertainty for the network's total
  (summed) demand.

```
SS_hub = Z * sqrt(LT_ext * σ_pooled² + D_sum² * σ_LT_ext²)
```

### This is a genuine trade-off, not a guaranteed win

The demand-variability term (`σ_pooled²`) benefits from pooling: combining
independent noise sources reduces relative variability (the classical
risk-pooling / Eppen centralization argument).

The lead-time-variability term (`D_sum² * σ_LT_ext²`) does **not** pool the
same way. Because it scales with the **square of the aggregated demand**,
and `(ΣDᵢ)² > Σ(Dᵢ²)` whenever more than one node has non-trivial demand,
concentrating the entire network's order into one node exposed to one
shared, uncertain lead time can make this term *larger* than the sum of
each node's individual exposure to that same uncertainty. Centralization
reduces **idiosyncratic** (demand) risk but concentrates **systemic**
(shared supply lead-time) risk.

**Practically**: this module reports whichever direction the numbers
actually show (`compare_single_vs_multi_echelon` in
`app/optimization/multi_echelon.py`), with an explicit verdict string
explaining why. In the shipped demo network, supplier lead-time variability
(CV ≈ 0.13-0.33 depending on supplier) is large enough relative to demand
variability that centralizing at `CENTRAL-DC` **increases** required
network safety stock for most SKUs - which is itself a genuine, useful
finding the tool surfaces (e.g. "reduce supplier lead-time variance via
dual-sourcing before centralizing," which the UI states).

This is **not** a full Guaranteed-Service or Stochastic-Service multi-echelon
optimization model (which would require a nonlinear solver over a DAG with
service-time decision variables, e.g. via OR-Tools). It is a documented,
explainable first-pass approximation appropriate for a first cut at network
safety-stock allocation, not a claim of optimality.

## Network rebalancing

Surplus/deficit classification compares each node's current days-of-supply
against a target. Deficits are matched against the largest surpluses for the
same SKU. Transit time and transport cost are estimated from haversine
distance between node coordinates (`app/optimization/network.py`,
`cost_per_km_per_unit` and `km_per_transit_day` are documented, configurable
default road-freight assumptions used only because no real transportation
rate card is loaded - see `docs/known-limitations.md`).

## Allocation

FIFO, priority-customer (weighted), proportional/fair-share, and margin-based
allocation are implemented as pure functions
(`app/optimization/allocation.py`) and applied to real open sales-order
demand for a SKU-location in `app/services/allocation_service.py`.

## Optimization infeasibility

`run_meio_comparison` returns `{"status": "INFEASIBLE", "reason": ...}`
rather than fabricating a result when the requested nodes don't exist or
have no demand history - see `test_optimization_infeasible_is_reported_not_fabricated`
in the test suite.
