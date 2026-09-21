# Architecture

## Layers

```
routes/        HTTP layer (Flask blueprints). Thin - parses request, calls a
                service, renders a template or returns JSON.
services/       Orchestration layer. Reads/writes the database, calls
                optimization/analytics modules, persists results
                (OptimizationRun, Recommendation, Alert, Incident, Scenario).
optimization/   Pure functions. No database access. Every function returns a
analytics/      dict that includes the formula, inputs and assumptions used,
                so the UI's "Calculation Explorer" pattern can show its work.
models/         SQLAlchemy ORM - the canonical data model (see data-model.md).
```

This separation means every optimization/analytics function is independently
unit-testable without a database (see `tests/test_safety_stock.py`,
`tests/test_eoq.py`, etc.), while services are tested against a real
in-memory SQLite database via `tests/test_inventory_service.py` and the
full-stack integration suite in `tests/test_integration_demo.py`.

## Request flow example: replenishment

```
GET /replenishment
  -> routes/replenishment.py: home()
     -> services/replenishment_service.generate_replenishment_recommendations(persist=False)
        -> for each ReplenishmentPolicy:
             services/demand_stats.daily_demand_stats()      (reads DemandHistory)
             services/inventory_service.inventory_position()  (reads InventoryLedger)
             optimization/reorder_point.reorder_point()       (pure calc)
             optimization/eoq.eoq() + apply_lot_size_constraints()  (pure calc)
             optimization/replenishment.rop_eoq_recommendation()    (pure calc)
     -> templates/replenishment/home.html
```

## Why no Celery/Redis/Kafka for the MVP

Section 159/198 of the brief explicitly asks for an architecture that
*allows* background jobs and event streaming later without *requiring* them
to run locally. Every "run" (optimization, alert generation, scenario) is
a synchronous request against a small, demo-scale dataset (dozens of SKUs x
single-digit locations) and completes in well under a second. The
`OptimizationRun`, `Alert`, `Incident`, `Recommendation` and `Scenario`
tables already look exactly like what a background worker would write to,
so introducing Celery/RQ later is additive, not a rewrite: a worker would
call the same `app/services/*` functions from a task queue and write to
the same tables.

## Event abstraction (section 114)

There is no message bus in this build. The nearest equivalent is the
`InventoryTransaction` append-only log table and the `AuditLog` table, which
record what happened and when. A real event bus (Kafka/EventBridge/Pub-Sub)
would publish the same shape of event; the model is deliberately named to
make that swap straightforward later (see `known-limitations.md`).

## Connector architecture (sections 83, 199)

`ERPConnector` / `WMSConnector` / `TMSConnector` interfaces are **not**
implemented as code in this build - there is nothing to connect to. Instead,
every place an external system would normally be called (PO/TO execution)
runs in **simulation mode** and says so explicitly in the UI and API
response (`"no live ERP/WMS/TMS connector configured"`), per the brief's
explicit instruction not to fake live integrations.
