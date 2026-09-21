# Data Model

Full model source: `app/models/__init__.py`. Designed for PostgreSQL in
production (`render.yaml`); SQLite locally.

## Master data
`Organization`, `User`, `Supplier`, `Customer`, `Location` (with
`parent_location_id` for the network hierarchy), `Item`, `UomConversion`,
`ExchangeRate`, `BomComponent`.

## Inventory truth
`InventoryLedger` — one row per (item, location, status, batch): the
canonical source of physical/available/allocated/in-transit/etc. balances.
Statuses: `ON_HAND, AVAILABLE, ALLOCATED, COMMITTED, QUARANTINED, BLOCKED,
DAMAGED, IN_TRANSIT, WIP, ON_ORDER, RETURN_IN_TRANSIT, RETURNED, REPAIR,
SCRAP, EXPIRED, EXCESS, OBSOLETE`.

`InventoryTransaction` — append-only movement log (receipt, shipment,
transfer, adjustment, return, scrap) for the transaction timeline / audit
trail.

## Demand & forecast
`DemandHistory` (actuals used for demand statistics), `ForecastPoint`
(forecast + P10/P90 + actual, for forecast-error tracking once forecasts are
loaded - not populated by the demo generator).

## Orders
`PurchaseOrder` + `PurchaseOrderLine`, `TransferOrder`, `SalesOrder` +
`SalesOrderLine`, `Shipment`.

## Policies
`SafetyStockPolicy` (calculated value + optional planner override with
reason, per section 143), `ReplenishmentPolicy` (min/max, ROP/EOQ, or
order-up-to).

## Optimization / scenarios
`OptimizationRun` (persists objective, input snapshot, result summary, and
runtime for reproducibility per section 163), `Scenario` (assumptions +
results JSON, never mutates live data).

## Exceptions / decisions
`Alert`, `Incident` (clusters related alerts with an observed vs. likely
root cause), `Recommendation` (with `reason_json` / `expected_impact_json`
for explainability, `confidence`, `autonomy_level`), `Approval`,
`Execution` (mode: SIMULATED — no LIVE mode exists because no connector is
configured), `AuditLog`.

## Deliberate simplifications vs. the full canonical model requested

- No separate `Batch`/`Lot`/`Serial` tables - batch/lot identity is carried
  as `batch_code` + `manufacture_date` + `expiry_date` directly on
  `InventoryLedger` rows. Sufficient for FEFO/aging/expiry logic; a
  dedicated genealogy table (supplier → batch → production → shipment →
  customer) is not implemented.
- No `ForecastError` table - forecast error would be computed from
  `ForecastPoint.forecast_qty` vs `.actual_qty`, but no forecast is loaded
  by the demo generator, so this is present in the schema but unexercised.
- Single-organization: `Organization`/`User` rows exist for attribution but
  there is no authentication gate (see `known-limitations.md`).
