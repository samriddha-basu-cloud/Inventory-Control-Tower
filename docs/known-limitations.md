# Known Limitations

Honest list of what this build does **not** do, so nobody mistakes scope for
a defect report later. The master specification for this project (208
sections) describes a much larger enterprise suite than a single build
session can deliver at production quality; this document is the map of the
gap.

## Not implemented at all

- **Live ERP/WMS/TMS/3PL/EDI connectors.** No credentials, no live calls.
  Every "execution" is simulated and says so. Connector *interfaces* are
  described in `docs/architecture.md` but no `ERPConnector` class ships in
  this build.
- **Authentication / authorization.** `User`/role exist as data, but there
  is no login, no session-based identity, no permission enforcement. Do not
  deploy this build's API/UI on the open internet without adding one.
- **PDF report export.** Only the multi-sheet Excel export is implemented.
- **Monte Carlo simulation** (section 103) and **Pareto-frontier
  multi-objective optimization** (section 182). Stockout probability uses a
  closed-form normal approximation instead (see `docs/inventory-methodology.md`).
- **ML-based lead-time / stockout / anomaly prediction** (sections 51, 137,
  138). `Supplier.otif_pct` / lead-time stats are used directly in
  formulas; no scikit-learn/XGBoost model is trained, because the demo
  dataset (24 SKUs, ~200 SKU-location combinations) is not large enough to
  train or validate one honestly. The brief explicitly says not to train
  models where data is insufficient (section 137) - this is that judgment
  call, made deliberately.
- **BOM explosion / component pegging / alternate components** (sections
  41-44). The `BomComponent` table exists; no service computes FG→component
  requirements or pegs supply to demand.
- **EDI 846/856/214 parsers** (section 115). Not built.
- **Command palette (Ctrl/Cmd+K full command execution)**. Search (Ctrl/Cmd+K
  focuses a search box) is implemented; a full command palette that can
  *execute* actions (not just navigate) is not.
- **Space/capacity optimization** (section 107), **cycle-count scheduling**
  (section 66), **reverse logistics / circular inventory pool** (sections
  63-64), **carbon/sustainability dashard beyond the cost model hooks**
  (sections 78-79, 150).
- **Database migrations** (Flask-Migrate/Alembic). `db.create_all()` creates
  missing tables but does not alter existing ones; changing a model after
  data exists requires a manual migration.

## Implemented but simplified

- **Multi-echelon optimization** is a documented two-tier risk-pooling
  approximation, not a full Guaranteed-Service Model solve. See
  `docs/optimization.md` for exactly what this means and its honest
  failure mode (it can recommend *against* centralizing).
- **Only one industry sample dataset** (FMCG) is generated. The industry
  profile registry (pharma/automotive/electronics/spare-parts) exists but
  their sample-data generators are not written.
- **Master data / dashboard is not industry-profile-aware yet** - the
  profile is stored but doesn't change what's rendered.
- **`AuditLog`** exists in the schema but is not written by every mutation
  path; only `Approval`/`Execution` are guaranteed audit records today.
- **No safety-stock override UI** - the override fields exist in the model
  and are respected by every calculation, but there's no form to set one
  yet (API/DB only).
- **Forecast integration** (`ForecastPoint`) exists in the schema but the
  demo generator does not populate it, so forecast-error-driven safety
  stock (section 96) is not exercised in the demo.
- **`.query.get()`** is used throughout instead of the SQLAlchemy-2.0-preferred
  `db.session.get()` - functionally correct (confirmed by the passing test
  suite) but produces `LegacyAPIWarning`s; a mechanical cleanup, not a bug.

## Recommended next development phase

1. Wire the industry-profile registry into the dashboard/nav so
   pharma/automotive/etc. actually look different, and write their sample
   datasets.
2. Add authentication + role-based UI emphasis (executive/planner/finance
   views already have their KPI sets identified in the brief; only the
   Control Tower is built today).
3. BOM explosion + component pegging for manufacturing/automotive/
   electronics modes.
4. A real transportation-lane rate table (replacing the haversine-distance
   cost proxy) once real lane data exists.
5. Flask-Migrate for schema evolution once this moves toward a second
   iteration of the data model.
