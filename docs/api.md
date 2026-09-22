# REST API

Base path `/api`. JSON in/out. Authentication: machine clients send `X-API-Key: <API_KEY env>` (bypasses CSRF and receives full permissions – protect the key);
browser sessions use the UI's RBAC and must send `X-CSRF-Token` on writes. All numbers are serialisable (NaN/inf become `null`).

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/inventory` | GET | Item-location positions (`sku`, `location`, `supplier`, `region`, `family`, `industry`, `limit`) |
| `/api/inventory/<sku>` | GET | One SKU across nodes + segmentation |
| `/api/location/<code>` | GET | Node capacity and all items stocked there |
| `/api/supplier/<code>` | GET | Supplier scorecard incl. risk-score components |
| `/api/demand` | GET | Weekly demand actuals |
| `/api/forecast` | GET / POST | Effective forecast / **ingest FIT forecast** (see fit-contract.md) |
| `/api/forecast/contract`, `/api/forecast/signals` | GET | Contract JSON / ICT→FIT demand signals & constraints |
| `/api/replenishment` | GET | Recommended orders (`all=1` for everything) |
| `/api/safety-stock` | GET | Method, z, SS, ROP and inputs per item-location |
| `/api/alerts`, `/api/incidents` | GET | Open alerts (with 8-question explainability) / root-cause incidents |
| `/api/scenarios` | GET | Stored scenarios and deltas |
| `/api/rebalancing`, `/api/optimization` | GET | LP transfer plan / MILP replenishment plan (status, objective table, constraints, infeasibility explanation) |
| `/api/actions` | GET / POST | List / create an action (runs simulation → policy check → autonomy decision) |
| `/api/approvals`, `/api/audit` | GET | Approval history / audit trail |
| `/api/events` | POST | Publish a canonical event (idempotent on `event_id`) |
| `/api/ingest/<entity>` | POST | `{"records":[…], "commit":true, "all_or_nothing":false}` for items, locations, suppliers, customers, item_suppliers, balances, transactions, demand, forecast, purchase_orders, sales_orders, lead_times, external_balances |
| `/api/search?q=` | GET | Global search (SKU, PO, SO, shipment, supplier, location, lot, incident) |
| `/api/health`, `/health` | GET | Liveness |

Errors: `{"error": "...", "status": 4xx}`; validation failures on ingestion return `207` with per-row `errors[]`. Stack traces are never returned.
