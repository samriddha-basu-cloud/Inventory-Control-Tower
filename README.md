# INVENTORY CONTROL TOWER (ICT)

*From Inventory Visibility to Autonomous Inventory Decisions.*

An all-industry, multi-echelon inventory intelligence and decision control tower: canonical inventory state, ingestion, ledger, reconciliation,
segmentation, probabilistic risk, safety stock / ROP / EOQ / 12 replenishment policies, alerts and root-cause incidents, a digital-twin scenario lab,
LP/MILP optimisation, governed actions with approvals and (mock) execution, reports and a REST API.

Stack: Flask 3 · Jinja2 · SQLAlchemy 2 (SQLite locally, PostgreSQL in production) · Alembic · NumPy/SciPy (HiGHS) · Plotly · vanilla JS.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt            # add requirements-dev.txt for tests
python run.py                              # http://127.0.0.1:5000  (macOS: port 5000 may be taken by AirPlay - use `PORT=5055 python run.py`)
```

The database (`instance/ict.db`), roles, 19 detection rules, KPI definitions and configuration are created on first start.
An empty system shows an onboarding screen: click **LOAD DEMO NETWORK** (pick industries; all seven take ~8 s).
CLI alternatives: `flask --app run.py init-db`, `flask --app run.py load-demo`, `flask --app run.py run-detection`.

```bash
pip install -r requirements-dev.txt && python -m pytest -q     # 186 passed, 1 skipped
```

Try your own data: `data/sample/*.csv` is a small fictional dataset (regenerate with `python data/sample/make_sample.py`). Upload in this order in
**Data Hub**: locations, suppliers, customers, items, item_suppliers, balances, demand, lead_times, purchase_orders, sales_orders.

Production: see [docs/deployment.md](docs/deployment.md) (`gunicorn wsgi:app`, `DATABASE_URL`, required `SECRET_KEY`, `AUTH_REQUIRED=1`, `API_KEY`). Variables: `.env.example`.

## What is implemented

| Area | Where |
|---|---|
| Canonical inventory states, Available / Net Available / Position (no double counting) | `app/services/engine.py`, `app/models/inventory.py` |
| Append-only ledger, idempotent postings, cycle counts, UOM conversion | `app/services/ledger_service.py` → `/inventory/ledger` |
| ERP/WMS/3PL/physical reconciliation with tolerances and suggested adjustments | `reconciliation_service.py` → `/inventory/reconciliation` |
| Data Hub: CSV/XLSX/JSON/REST ingestion with column mapping, validation, preview, all-or-nothing option, EDI 846/856/214 translation, canonical events | `ingestion_service.py`, `connectors/`, `event_service.py` → `/data-hub` |
| ABC/XYZ/FSN/HML/VED/SDE segmentation (configurable), aging, expiry & FEFO, lot/batch traceability (forward & backward) | `/inventory/*` |
| ICT Inventory Health Index (transparent, not an industry standard) | `/inventory/health` |
| Demand & forecast (Baseline/Consensus/Adjusted), FIT ⇄ ICT contract, forecast → inventory chain | `/demand`, `/demand/chain`, [docs/fit-contract.md](docs/fit-contract.md) |
| 8 safety-stock methods, ROP, EOQ, practical order quantity, 12 replenishment policies, lead-time static vs observed P90 comparison | `/safety-stock`, `/replenishment`, `/lead-time` |
| Time-phased projection, stock-out probability, expected shortage/lost sales, supplier scorecards and risk, supply visibility (PO/shipment/port/lane) | `/risk`, `/supply` |
| Pegging & allocation (no double commitment, scarcity simulator) | `/pegging` |
| Detection rules (JSON, safe engine), alert lifecycle, dedupe/suppression/escalation, root-cause incidents by shared dimensions, Planner Priority Score | `/alerts`, `/root-cause` |
| Scenario Lab / digital twin (16 change types, Monte-Carlo, never touches production), scenario compare, experiments, S&OP | `/scenarios`, `/digital-twin`, `/sop` |
| Optimisation: MILP replenishment (MOQ, multiples, budget, capacity, shelf-life, service floor), LP rebalancing, infeasibility explanations | `/optimization`, `/rebalancing`, `app/optimization/` |
| Financial (working capital, carrying cost, obsolescence) and sustainability (carbon *estimates*) | `/financial`, `/sustainability` |
| Industry modes (automotive, pharma, retail/FMCG, high-tech, manufacturing, spare parts) | `/industry`, [docs/industry-modes.md](docs/industry-modes.md) |
| Action Center, autonomy levels 0–4 with guardrails, HITL approvals, MockExecutionConnector, audit trail | `/actions`, `/autonomy`, `/audit`, `/governance`, [docs/governance.md](docs/governance.md) |
| Reports: 11 reports as CSV/XLSX/PDF + 18-sheet workbook | `/reports`, `/reports/full-workbook.xlsx` |
| REST API (22 routes), CSRF, RBAC, API key, secure uploads | [docs/api.md](docs/api.md) |
| Master data, configurable policy hierarchy (GLOBAL→SKU_LOCATION), KPI formula editor, settings | `/master-data`, `/settings` |
| Global filters, global search (`/` focuses it), drill-down, light/dark, responsive layout | `app/templates/base.html`, `app/static/` |

Formulas: [docs/inventory-methodology.md](docs/inventory-methodology.md). Architecture: [docs/architecture.md](docs/architecture.md). Data model: [docs/data-model.md](docs/data-model.md).
User guide: [docs/user-guide.md](docs/user-guide.md).

## Project structure

```
app/
  __init__.py  config.py  extensions.py
  models/        canonical data model (master, inventory, orders, planning, control)
  services/      engine (pure calc), snapshot, ledger, ingestion, alerts, incidents, simulation, actions, reports, demo generator …
  optimization/  SciPy/HiGHS solvers (replenishment MILP, transfer LP, allocation LP, network)
  connectors/    EDI translator, connector interfaces + mock adapters, notifications
  routes/        18 blueprints (UI + /api)
  templates/ static/ utils/
data/sample/     CSV sample for the Data Hub          migrations/   Alembic
docs/            methodology, API, FIT contract, governance, deployment, limitations
tests/           186 tests (calculations, ledger/recon, optimisation, alerts/actions, data + web)
```

## Requires external integrations or credentials (not included / not live)

* **ERP / WMS / TMS / MES / 3PL / IoT / telematics** – SAP, Oracle, Dynamics, NetSuite etc. are listed in the connector catalogue as *NOT CONFIGURED*. Data enters by file upload, REST
  (`/api/ingest/<entity>`, `/api/events`, `/api/forecast`) or mock adapters. Write-back is not implemented; `LIVE` execution is refused.
* **EDI transport** (AS2/VAN/SFTP) – ICT translates pasted/uploaded X12 846/856/214 text only. Mappings are simplified; adapt to your partner guides.
* **Message bus** – events are stored and processed in-process; `EventPublisher` is the seam for Kafka/Event Hubs.
* **Notifications** – Email (SMTP), Slack, Teams, webhook are off until `SMTP_*`, `NOTIFY_*_URL` are set.
* **FIT** – sends forecasts with `X-API-Key` (`API_KEY`).
* **PostgreSQL** – `DATABASE_URL` and `pip install psycopg2-binary`. **Auth** – `AUTH_REQUIRED=1`, `ADMIN_PASSWORD`, `SECRET_KEY`. For SSO/LDAP integrate at `load_user`.
* **Background jobs at scale** – Celery/RQ/Redis can replace `services/jobs.py`; the default runs jobs inline (or a thread pool with `JOBS_SYNC=0`).

## Limitations (details in [docs/known-limitations.md](docs/known-limitations.md))

Demo data is synthetic; the demo runs on mock execution only; probabilities assume normal demand within an interval and a mixture over the four most uncertain arrivals;
the projection shows scheduled supply only; simulation and optimisation are decision-support approximations (linear cost proxies, per-item transfer LP); carbon is estimated
from configurable factors; the PDF report is a summary-table export; no FX conversion; alert thresholds need tuning on real data.
