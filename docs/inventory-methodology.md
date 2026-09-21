# Inventory Methodology Reference

All formulas below are implemented in `app/optimization/` and `app/analytics/`
and covered by unit tests in `tests/`. Every function that computes one of
these returns its formula and inputs alongside the result.

## Inventory Position
```
Inventory Position = On Hand + On Order + In Transit - Allocated - Backorders
```
Configurable in principle (the field exists on `Organization.inventory_position_formula`);
the current build computes this one fixed formula from ledger buckets. See
`app/services/inventory_service.py::inventory_position`.

## Available-To-Promise (ATP)
```
ATP = Available Now + Future Receipts (open POs) - Open Demand (open SO lines)
Available Now = ON_HAND - (ALLOCATED + COMMITTED + QUARANTINED + BLOCKED + DAMAGED)
```

## Safety Stock (7 methods, `app/optimization/safety_stock.py`)
- **Basic statistical**: `SS = Z * σ_D`
- **Demand variability**: `SS = Z * σ_D * sqrt(LT)`
- **Lead-time variability**: `SS = Z * D̄ * σ_LT`
- **Combined variability** (default): `SS = Z * sqrt(LT*σ_D² + D̄²*σ_LT²)`
- **Periodic review**: same as combined, with `LT` replaced by `R + LT` (protection period)
- **Continuous review**: combined variability with `R = 0`
- **Seasonal**: `SS_seasonal = SS_base * SeasonalIndex`

`Z` is derived from the target cycle service level via the inverse standard
normal CDF (`scipy.stats.norm.ppf`).

## Reorder Point
```
ROP = (D̄ * LT) + SS
```

## Economic Order Quantity
```
EOQ = sqrt(2 * D * S / H)
```
then reconciled against MOQ, order multiple and (optionally) a capacity
constraint via `apply_lot_size_constraints`, which reports every step (e.g.
"raised to MOQ", "rounded up to order multiple").

## Days of Supply / Turns / DIO
```
DOS   = Available Inventory / Average Daily Demand
Turns = Annualized COGS / Average Inventory Value
DIO   = (Average Inventory Value / Annualized COGS) * 365
```

## ABC / XYZ
- **ABC**: Pareto split on annual consumption value. Default cutoffs: A = top
  80% cumulative value, B = next 15%, C = remaining 5%.
- **XYZ**: coefficient of variation of demand. Default: X ≤ 0.5, Y ≤ 1.0,
  Z > 1.0. A SKU with no reliable demand signal is treated as Z (highest
  risk), not silently dropped.

## FIT Inventory Health Index
A documented, platform-defined composite (0-100), **not** claimed to be an
industry-standard score:
```
FIT = 100 - [25% * stockout_exposure + 20% * excess_exposure
            + 15% * obsolescence_exposure + 20% * service_level_gap
            + 10% * inventory_accuracy_gap + 10% * lead_time_risk]
```
Weights are configurable (`app/analytics/inventory_health.py`).

## Stockout probability
Demand during lead time is modeled as approximately normal with mean
`D̄ * LT` and variance `LT*σ_D² + D̄²*σ_LT²` (the same variance term used for
combined-variability safety stock). Stockout probability = `P(available <
lead-time demand)` via the normal CDF. This is an approximation - see
`app/services/risk_service.py::stockout_risk`.

## Excess detection
A SKU-location is flagged excess only when available stock exceeds
`excess_multiple × (safety stock + lead-time demand)` (default multiple:
2.0) - **not** merely for exceeding safety stock, per the brief's explicit
instruction (section 55).

## Multi-Echelon Inventory Optimization
See [`optimization.md`](optimization.md) for the full derivation, including
the documented trade-off between demand-variability pooling (which reduces
safety stock) and shared lead-time-variability concentration (which can
increase it).
