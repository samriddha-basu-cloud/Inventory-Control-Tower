# Canonical data model

Every major entity carries `source_system, source_id, last_updated, effective_date, status` (`CanonicalMixin`) so multiple systems can be harmonised.

| Group | Tables |
|---|---|
| Master | `item` (SKU/product), `product_family`, `item_uom`, `location` (plant/DC/warehouse/store/line-side…, self-referencing `parent_id`, `echelon`), `supplier`, `customer`, `carrier`, `item_supplier`, `item_location_source`, `bom_line`, `calendar_event`, `promotion` |
| Inventory | `inventory_balance` (item × location × lot × state), `inventory_transaction` (append-only ledger), `lot` (lot/batch), `serial_number`, `external_balance` (ERP/WMS/3PL/physical), `reusable_asset`, `return_record`, `kpi_snapshot` |
| Orders | `purchase_order(+_line)`, `sales_order(+_line)`, `transfer_order`, `production_order`, `shipment(+_line)` |
| Planning | `demand`, `forecast` (BASELINE/CONSENSUS/ADJUSTED, source FIT/ERP/UPLOAD/MANUAL), `lead_time_observation`, `safety_stock_policy`, `replenishment_policy`, `control_policy`, `allocation`, `peg` |
| Control | `alert`, `incident`, `risk`, `recommendation`, `action`, `approval`, `execution`, `autonomy_rule`, `scenario`, `experiment`, `event`, `sync_status`, `job`, `notification`, `audit_log` |
| Configuration | `setting`, `industry_profile`, `inventory_state_def`, `rule`, `kpi_definition`, `role`, `user_account` |

Entity relationships that matter: an *item-location pair* is the unit of planning; inbound supply is derived from open PO lines
(`on_order = ordered − received − in_transit`), transfers and production orders (no double counting with shipments, which are visibility records);
`peg` links supply to demand; `allocation` rows are the only claims on stock.

Migrations: `migrations/` (Alembic via Flask-Migrate). Local runs use `db.create_all()` + seeding for convenience; production should run `flask db upgrade`.
