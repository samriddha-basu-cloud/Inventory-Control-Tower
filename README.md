# Inventory Control Tower (ICT)

*"See. Predict. Optimize. Execute."*

A working, multi-industry inventory intelligence and decision platform: visibility (ledger,
ATP, inventory position), analytics (ABC/XYZ, aging, service level, lead-time statistics),
optimization (safety stock, ROP, EOQ, single- vs multi-echelon, allocation, rebalancing),
risk/exception management (alerts, incident clustering, root cause), scenario simulation
(digital twin), and a human-in-the-loop recommend → approve → execute workflow.

This README describes exactly what is implemented and runnable today. See
[`docs/known-limitations.md`](docs/known-limitations.md) for what is explicitly deferred.

## Quick start (local)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt        # add requirements-dev.txt too if you want to run tests
cp .env.example .env                   # edit SECRET_KEY etc. if you like
python run.py                          # http://localhost:5000
```

The app creates its SQLite database and tables automatically on first request
(`instance/ict.db`). From the UI, click **Launch Demo** (top right, on every
page) to load the synthetic multi-echelon FMCG network described below.

Equivalent from the CLI:

```bash
flask --app run init-db      # create tables only
flask --app run load-demo    # create tables + load synthetic demo network
```

## Run the tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

105 tests cover the safety-stock/ROP/EOQ formulas (including zero-demand,
zero-lead-time, zero-variance and negative-input edge cases), ABC/XYZ
classification, aging/FEFO/FIFO, UOM and currency conversion, allocation
rules, single- vs multi-echelon comparison, inventory position/ATP against a
real database, and a full integration suite that loads the demo network and
exercises every page and API endpoint.

## What's actually implemented

- **Canonical inventory ledger** with the full status vocabulary from the
  spec (ON_HAND, ALLOCATED, COMMITTED, QUARANTINED, IN_TRANSIT, ON_ORDER,
  EXCESS, OBSOLETE, …) — see `app/models/__init__.py`.
- **Inventory position & ATP**, computed from real ledger + open PO/SO data,
  never conflating on-hand with available (`app/services/inventory_service.py`).
- **ABC / XYZ / segment policy recommendations**, computed network-wide from
  trailing demand history (`app/analytics/abc_xyz.py`).
- **Aging, FEFO/FIFO, expiry tracking** including near-expiry/expired batch
  detection (`app/analytics/aging.py`).
- **Safety stock engine** with 7 documented methodologies (basic statistical,
  demand variability, lead-time variability, combined variability, periodic
  review, continuous review, seasonal) — every result shows its formula,
  inputs and assumptions (`app/optimization/safety_stock.py`).
- **ROP, EOQ, and lot-size reconciliation** (MOQ / order multiple / capacity)
  with a step-by-step explanation of the final recommended quantity.
- **Single-echelon vs multi-echelon (MEIO) comparison** using a documented
  risk-pooling approximation for a two-tier network. This is genuinely
  bidirectional — see [`docs/optimization.md`](docs/optimization.md) for why
  centralizing safety stock can *increase* it when supplier lead-time
  variability dominates, and the UI reports whichever direction the numbers
  actually show.
- **Network rebalancing** — surplus/deficit detection and transfer
  recommendations with haversine-distance-based transit time & cost.
- **Allocation engine** — FIFO, priority-customer, proportional/fair-share
  and margin-based allocation against real open sales-order demand.
- **Stockout risk & excess/obsolescence detection**, probability-based using
  demand + lead-time variance, not a flat "below safety stock" rule.
- **Alert engine** with severity scoring (P1–P4), and clustering of related
  alerts into one Incident with an observed vs. likely (never fabricated)
  root cause.
- **Recommendation → Approval → Execution workflow** with a full audit trail
  (`Recommendation`, `Approval`, `Execution` tables) and an execution-safety
  check that re-validates availability before simulated execution.
- **Digital-twin scenarios** — demand/lead-time/safety-stock what-ifs
  computed from a snapshot, never mutating live data; scenarios are
  versioned and comparable.
- **Supplier risk scoring**, **FIT Inventory Health Index** (documented
  composite, not claimed to be an industry standard).
- **Network map** (Plotly) and **SKU × location heatmap** (DOS / value /
  stockout risk).
- **Master Data Center** with a real data-quality scan (missing supplier,
  missing/invalid MOQ, missing lead time, duplicate SKU).
- **REST API** (`/api/...`) for inventory position/availability/risk/aging,
  optimization runs, scenarios, allocation, replenishment, alerts and
  approvals — see [`docs/api.md`](docs/api.md).
- **Multi-sheet Excel report export** (README, Executive Summary, Inventory
  Position, Aging, ABC/XYZ, Safety Stock, Stockout Risk, Excess, Supplier,
  Alerts, Methodology).
- **Synthetic demo data generator**: 4 suppliers, 1 plant, 1 central DC, 3
  regional DCs, 4 stores, 5 customers, 24 SKUs, 90 days of demand history,
  and a deliberately injected supplier-delay disruption so the alert →
  incident → rebalancing → approval → execution story is demonstrable
  end-to-end (see `app/services/demo_data_service.py`).

## Repository structure

```
app/
  routes/         Flask blueprints (dashboard, inventory, network, optimization,
                   alerts, scenarios, allocation, replenishment, reports,
                   master_data, api)
  models/          SQLAlchemy canonical data model
  services/        Business logic tying models + optimization/analytics together
  optimization/     Pure calculation modules: safety_stock, reorder_point, eoq,
                   single_echelon, multi_echelon, allocation, replenishment, network
  analytics/        abc_xyz, aging, service_level, lead_time, working_capital,
                   inventory_health
  templates/, static/   UI
  utils/           uom.py, currency.py, industry_profiles.py
tests/             pytest suite (105 tests)
docs/              architecture, methodology, API, deployment, governance, limitations
```

## Deployment (Render)

`render.yaml` provisions a Postgres database and a web service running
`gunicorn`. See [`docs/deployment.md`](docs/deployment.md).

## Industry modes

The platform ships one shared codebase with an **industry configuration
layer** (`app/utils/industry_profiles.py`) rather than a forked codebase per
vertical — see [`docs/industry-modes.md`](docs/industry-modes.md).

## Known limitations

See [`docs/known-limitations.md`](docs/known-limitations.md) for an honest
list of what is deferred (PDF export, live ERP/WMS/TMS connectors, real
authentication, Monte Carlo simulation, ML-based lead-time prediction, and
more) and why.
