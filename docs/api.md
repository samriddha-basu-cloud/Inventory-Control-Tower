# REST API

Base path: `/api`. JSON in/out. No authentication (see
`known-limitations.md`) — do not expose this build's API on the open
internet without adding one.

Not yet OpenAPI-annotated; this document is the contract for now.

| Method | Path | Description |
|---|---|---|
| GET | `/api/inventory` | All SKU-location status quantities (capped at 500 items) |
| GET | `/api/inventory/position?item_id=&location_id=` | Inventory position breakdown |
| GET | `/api/inventory/availability?item_id=&location_id=` | Available quantity + ATP |
| GET | `/api/inventory/risk?item_id=&location_id=` | Stockout risk + excess detection |
| GET | `/api/inventory/aging?item_id=` | Aging bucket summary (optionally filtered) |
| POST | `/api/optimization/run` | `{"run_type": "safety_stock"\|"meio"\|"rebalancing", ...}` |
| POST | `/api/scenario/run` | `{"name": "...", "assumptions": {"demand_change_pct": 10, ...}}` |
| POST | `/api/allocation/recommend` | `{"item_id": 1, "location_id": 2, "rule": "priority_customer"}` |
| POST | `/api/replenishment/recommend` | `{"persist": false}` |
| GET | `/api/alerts?status=NEW` | List alerts, optional status filter |
| POST | `/api/actions/approve` | `{"recommendation_id": 1, "decision": "APPROVE"}` |

All endpoints requiring `item_id`/`location_id` return HTTP 400 with a JSON
`{"error": "..."}` body (not a raw stack trace) when they're missing.

## Example

```bash
curl -X POST http://localhost:5000/api/optimization/run \
  -H "Content-Type: application/json" \
  -d '{"run_type": "meio", "central": "CENTRAL-DC", "regionals": ["DC-NORTH","DC-SOUTH"]}'
```
