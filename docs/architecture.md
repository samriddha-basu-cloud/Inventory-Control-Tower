# Architecture

```
Browser (Jinja2 + vanilla JS + Plotly)         REST clients (FIT, ERP push)  ── X-API-Key
              │                                        │
       app/routes  (18 blueprints, RBAC, CSRF)   app/routes/api.py
              │
       app/services ──────────────────────────────────────────────────────────────┐
   snapshot.py  DB rows → PairInputs (plain dataclasses, cached per data-version) │
   engine.py    PairInputs → PairResult   (pure functions: demand, lead time, SS, │
                ROP/EOQ, position, projection, risk, excess, confidence)          │
   alert / incident / risk / kpi / health / reconciliation / financial / carbon   │
   allocation / optimization / simulation / action / event / ingestion / report   │
              │                         │                                         │
       app/optimization (SciPy HiGHS)   app/rules (rule engine, policies, thresholds)
              │                                                                   │
       app/connectors  (mock ERP/WMS/TMS, EDI 846/856/214, MockExecutionConnector, notifiers)
              │
       app/models (SQLAlchemy 2)  ── SQLite locally / PostgreSQL in production ── Alembic migrations
```

**Design rules**

1. *Engines are pure.* `engine.compute_pair`, `projection_service`, `safety_stock_service`, … take dataclasses and return dataclasses; they never touch the
   database or Flask. That is what makes the digital twin safe (it deep-copies `PairInputs`) and the maths unit-testable.
2. *One snapshot.* All dashboards, APIs, alerts, scenarios and reports read the same cached snapshot, so numbers agree across pages. The cache is keyed by a
   data-version counter stored in the DB (`system.data_version`), bumped on every write in any worker.
3. *Configuration is data.* Rules (`Rule`), KPIs (`KpiDefinition`), policies, autonomy rules, roles, inventory states and settings are rows. Rules are JSON
   conditions evaluated by a safe interpreter; KPI formulas by an AST whitelist evaluator.
4. *History is append-only.* The inventory ledger stores before/after on-hand per transaction; corrections are reversals. The audit log records data loads,
   calculation traces, configuration changes, actions, approvals and executions.
5. *Humans stay in the loop.* Detect → analyse → options → **simulate** → **policy check** → autonomy/approval → execute (mock) → verify → audit. Execution mode
   defaults to `SIMULATION_ONLY`; `LIVE` is refused without a real connector.
6. *Event-ready.* `event_service` validates 14 canonical events, is idempotent on `event_id` and dispatches synchronously through `EventPublisher` – swap in a
   Kafka/Event Hub publisher without touching handlers.
7. *Background-ready.* `jobs.submit` runs heavy work inline by default (`JOBS_SYNC=1`) or on a thread pool (`JOBS_SYNC=0`); replace with Celery/RQ behind the same interface.

## Module map (spec A–AD)

| Spec module | Where |
|---|---|
| Control tower, filters, search, drill-down | `routes/main.py`, `templates/control`, `services/network_service.py`, `services/search_service.py` |
| Data hub / ingestion / EDI / events | `routes/datahub.py`, `services/ingestion_service.py`, `connectors/edi.py`, `services/event_service.py` |
| Master data, UOM, quality | `routes/master.py`, `utils/uom.py`, `services/master_data_service.py` |
| Ledger, reconciliation, traceability, aging, expiry | `services/ledger_service.py`, `reconciliation_service.py`, `traceability_service.py`, `expiry_service.py` |
| Replenishment, safety stock, lead time | `services/replenishment_service.py`, `safety_stock_service.py`, `lead_time_service.py` |
| Optimization, rebalancing | `optimization/*`, `services/optimization_service.py` |
| Pegging, allocation | `services/allocation_service.py`, `optimization/allocation.py` |
| Risk, alerts, root cause | `services/risk_service.py`, `alert_service.py`, `incident_service.py` |
| Digital twin, scenarios, S&OP, experiments | `services/simulation_service.py`, `sop_service.py`, `experiment_service.py` |
| Financial, carbon, circular | `services/financial_service.py`, `carbon_service.py`, `routes/finance.py` |
| Industry modes | `services/industry_service.py`, `services/seed_service.py` (profiles) |
| Actions, autonomy, approvals, connectors | `services/action_service.py`, `connectors/execution.py` |
| Governance, audit, reports | `routes/governance.py`, `services/audit_service.py`, `services/report_service.py` |
