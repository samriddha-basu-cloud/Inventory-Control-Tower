"""
Synthetic demo data generator (sections 164-166).

Builds a small but realistic multi-echelon FMCG network — 4 suppliers, 1
plant, 1 central DC, 3 regional DCs, 4 stores, 5 customers, 24 SKUs — with
90 days of daily demand history, live inventory across ledger states, open
purchase/transfer/sales orders, and one deliberately-injected supplier-delay
disruption so the Control Tower demo tells the section-166 story end to end:

  Supplier delay -> late PO -> DC-SOUTH projected shortage for 3 SKUs ->
  Control Tower detects it (alert engine) -> clusters into one incident ->
  rebalancing engine finds DC-NORTH surplus for the same SKUs -> transfer
  recommendation -> financial impact shown -> (planner approves in UI) ->
  simulated execution.

All data here is synthetic. No proprietary or real company data is used.
"""
import random
from datetime import date, timedelta

from app.extensions import db
from app.models import (
    Organization, User, Supplier, Customer, Location, Item, UomConversion, ExchangeRate,
    InventoryLedger, DemandHistory, PurchaseOrder, PurchaseOrderLine, TransferOrder,
    SalesOrder, SalesOrderLine, SafetyStockPolicy, ReplenishmentPolicy,
)

RNG_SEED = 42

CATEGORIES = ["Beverages", "Snacks", "Personal Care", "Home Care"]


def _reset_all():
    for model in [SalesOrderLine, SalesOrder, PurchaseOrderLine, PurchaseOrder, TransferOrder,
                  DemandHistory, InventoryLedger, SafetyStockPolicy, ReplenishmentPolicy,
                  UomConversion, ExchangeRate, Item, Customer, Supplier, Location, User, Organization]:
        model.query.delete()
    db.session.commit()


def load_demo_data(reset=True):
    rng = random.Random(RNG_SEED)
    if reset:
        _reset_all()

    org = Organization(name="Northstar Consumer Goods (Demo)", base_currency="INR", industry_profile="fmcg")
    db.session.add(org)
    db.session.add(User(name="Priya Sharma", email="priya.planner@demo.org", role="planner"))
    db.session.add(User(name="Arjun Rao", email="arjun.exec@demo.org", role="executive"))
    db.session.add(User(name="Meera Iyer", email="meera.finance@demo.org", role="finance"))
    db.session.flush()

    # --- Suppliers ------------------------------------------------------------
    suppliers = [
        Supplier(code="SUP-01", name="Coastal Packaging Ltd", country="India", tier=1,
                 otif_pct=94, defect_rate_pct=0.8, single_source_risk=False,
                 lead_time_mean_days=10, lead_time_std_days=1.2),
        Supplier(code="SUP-02", name="Highland Ingredients Co", country="India", tier=1,
                 otif_pct=91, defect_rate_pct=1.2, single_source_risk=False,
                 lead_time_mean_days=14, lead_time_std_days=1.8),
        Supplier(code="SUP-03", name="Global Chem Traders", country="Vietnam", tier=2,
                 otif_pct=78, defect_rate_pct=2.1, single_source_risk=True,
                 lead_time_mean_days=21, lead_time_std_days=7),  # the disruption supplier - deliberately high variability
        Supplier(code="SUP-04", name="Riverside Bottling Partners", country="India", tier=1,
                 otif_pct=96, defect_rate_pct=0.5, single_source_risk=False,
                 lead_time_mean_days=8, lead_time_std_days=1.0),
    ]
    db.session.add_all(suppliers)
    db.session.flush()

    # --- Locations (network) ---------------------------------------------------
    plant = Location(code="PLANT-1", name="Chennai Manufacturing Plant", node_type="plant", region="South",
                      latitude=13.08, longitude=80.27, storage_capacity_units=500000, review_period_days=7)
    central = Location(code="CENTRAL-DC", name="Central Distribution Center", node_type="dc", region="National",
                        latitude=19.07, longitude=72.87, storage_capacity_units=400000, review_period_days=7)
    db.session.add_all([plant, central])
    db.session.flush()

    regionals = [
        Location(code="DC-NORTH", name="North Regional DC", node_type="dc", region="North",
                  latitude=28.61, longitude=77.20, storage_capacity_units=150000, review_period_days=5,
                  parent_location_id=central.id),
        Location(code="DC-SOUTH", name="South Regional DC", node_type="dc", region="South",
                  latitude=12.97, longitude=77.59, storage_capacity_units=150000, review_period_days=5,
                  parent_location_id=central.id),
        Location(code="DC-WEST", name="West Regional DC", node_type="dc", region="West",
                  latitude=19.22, longitude=72.97, storage_capacity_units=150000, review_period_days=5,
                  parent_location_id=central.id),
    ]
    db.session.add_all(regionals)
    db.session.flush()
    dc_north, dc_south, dc_west = regionals

    stores = [
        Location(code="STORE-DEL-01", name="Delhi Flagship Store", node_type="store", region="North",
                  latitude=28.65, longitude=77.23, storage_capacity_units=8000, review_period_days=3,
                  parent_location_id=dc_north.id),
        Location(code="STORE-BLR-01", name="Bengaluru Store", node_type="store", region="South",
                  latitude=12.97, longitude=77.60, storage_capacity_units=8000, review_period_days=3,
                  parent_location_id=dc_south.id),
        Location(code="STORE-CHN-01", name="Chennai Store", node_type="store", region="South",
                  latitude=13.05, longitude=80.21, storage_capacity_units=8000, review_period_days=3,
                  parent_location_id=dc_south.id),
        Location(code="STORE-MUM-01", name="Mumbai Store", node_type="store", region="West",
                  latitude=19.08, longitude=72.88, storage_capacity_units=8000, review_period_days=3,
                  parent_location_id=dc_west.id),
    ]
    db.session.add_all(stores)
    db.session.flush()

    demand_locations = regionals + stores  # locations that carry demand history / safety stock policy

    # --- Customers ---------------------------------------------------------
    customers = [
        Customer(code="CUST-01", name="MetroMart Retail Chain", segment="strategic", priority_weight=3.0,
                 region="North", service_level_target_pct=98),
        Customer(code="CUST-02", name="QuickStop Convenience", segment="standard", priority_weight=1.5,
                 region="South", service_level_target_pct=95),
        Customer(code="CUST-03", name="ValueBasket Stores", segment="standard", priority_weight=1.0,
                 region="South", service_level_target_pct=93),
        Customer(code="CUST-04", name="Coastal Wholesale", segment="opportunistic", priority_weight=0.7,
                 region="West", service_level_target_pct=90),
        Customer(code="CUST-05", name="Everyday Essentials Co-op", segment="standard", priority_weight=1.2,
                 region="North", service_level_target_pct=95),
    ]
    db.session.add_all(customers)
    db.session.flush()

    # --- Items ---------------------------------------------------------------
    items = []
    sku_num = 1000
    for cat_idx, category in enumerate(CATEGORIES):
        for i in range(6):
            sku_num += 1
            supplier = suppliers[(cat_idx + i) % len(suppliers)]
            unit_cost = round(rng.uniform(30, 600), 2)
            margin = rng.uniform(1.25, 1.9)
            shelf_life = rng.choice([None, None, None, 180, 270, 365]) if category != "Beverages" else rng.choice([None, 120, 180])
            item = Item(
                sku=f"SKU-{sku_num}", name=f"{category} Product {i+1}", category=category,
                product_family=category, base_uom="EA", unit_cost=unit_cost,
                unit_price=round(unit_cost * margin, 2), currency="INR",
                moq=rng.choice([500, 1000, 1500, 2000]), order_multiple=rng.choice([50, 100, 250]),
                shelf_life_days=shelf_life, is_batch_tracked=shelf_life is not None,
                primary_supplier_id=supplier.id,
                service_level_target_pct=rng.choice([90, 95, 97, 99]),
                ved_class=rng.choice(["V", "E", "D", "D"]),
            )
            items.append(item)
    db.session.add_all(items)
    db.session.flush()

    # Multi-UOM example: first item also sold by the case (1 CASE = 24 EA)
    db.session.add(UomConversion(item_id=items[0].id, from_uom="CASE", to_uom="EA", factor=24))
    # Multi-currency example: register a USD->INR rate for reporting valuation
    db.session.add(ExchangeRate(from_currency="USD", to_currency="INR", rate=83.2, as_of=date.today()))

    # --- Demand history (90 days) at each demand-facing location --------------
    today = date.today()
    # SKUs that will be the disruption's "affected" set (sourced from SUP-03, sold at DC-SOUTH)
    disrupted_supplier = suppliers[2]  # SUP-03, single-source, high variability
    disrupted_items = [it for it in items if it.primary_supplier_id == disrupted_supplier.id][:3]

    for item in items:
        # ABC/XYZ spread: vary base volume and variability by item hash
        base_volume = rng.uniform(5, 120)
        cv = rng.choice([0.15, 0.25, 0.4, 0.6, 0.9])  # low->high variability spread across X/Y/Z
        for loc in demand_locations:
            loc_factor = rng.uniform(0.4, 1.3)
            mean_demand = base_volume * loc_factor
            for d in range(90):
                day = today - timedelta(days=90 - d)
                # light weekly seasonality
                weekday_factor = 1.15 if day.weekday() in (4, 5) else 0.95
                qty = max(0, rng.gauss(mean_demand * weekday_factor, mean_demand * cv))
                db.session.add(DemandHistory(item_id=item.id, location_id=loc.id, period_date=day,
                                              quantity=round(qty, 1), revenue=round(qty * item.unit_price, 2)))
    db.session.flush()

    # --- Safety stock & replenishment policies --------------------------------
    for item in items:
        for loc in demand_locations:
            db.session.add(SafetyStockPolicy(
                item_id=item.id, location_id=loc.id, method="combined_variability",
                service_level_pct=item.service_level_target_pct, review_period_days=loc.review_period_days,
                calculated_safety_stock=0.0,
            ))
            db.session.add(ReplenishmentPolicy(
                item_id=item.id, location_id=loc.id, policy_type="rop_eoq",
            ))
    db.session.flush()

    # --- Inventory ledger: seed on-hand + pipeline positions -------------------
    for item in items:
        # Plant: WIP + some on-hand raw->finished
        db.session.add(InventoryLedger(item_id=item.id, location_id=plant.id, status="ON_HAND",
                                        quantity=round(rng.uniform(2000, 6000), 0)))
        db.session.add(InventoryLedger(item_id=item.id, location_id=plant.id, status="WIP",
                                        quantity=round(rng.uniform(200, 1000), 0)))
        # Central DC: healthy buffer
        db.session.add(InventoryLedger(item_id=item.id, location_id=central.id, status="ON_HAND",
                                        quantity=round(rng.uniform(3000, 9000), 0)))
        db.session.add(InventoryLedger(item_id=item.id, location_id=central.id, status="ALLOCATED",
                                        quantity=round(rng.uniform(100, 400), 0)))

        for loc in demand_locations:
            d_row = DemandHistory.query.filter_by(item_id=item.id, location_id=loc.id).order_by(
                DemandHistory.period_date.desc()).first()
            avg_demand = (d_row.quantity if d_row else 10)
            lt = item.primary_supplier.lead_time_mean_days if item.primary_supplier else 14
            target_stock = avg_demand * (lt + loc.review_period_days) * 1.3

            if item in disrupted_items and loc.code == "DC-SOUTH":
                on_hand = target_stock * 0.15   # deliberately deficit -> stockout story
            elif item in disrupted_items and loc.code == "DC-NORTH":
                on_hand = target_stock * 2.4    # deliberately surplus -> rebalancing source
            else:
                on_hand = target_stock * rng.uniform(0.7, 1.4)

            db.session.add(InventoryLedger(item_id=item.id, location_id=loc.id, status="ON_HAND",
                                            quantity=round(max(on_hand, 0), 0)))
            db.session.add(InventoryLedger(item_id=item.id, location_id=loc.id, status="ALLOCATED",
                                            quantity=round(avg_demand * rng.uniform(0.5, 2), 0)))

            if item.is_batch_tracked:
                expiry = today + timedelta(days=rng.choice([15, 25, 200, 300]))
                db.session.add(InventoryLedger(item_id=item.id, location_id=loc.id, status="ON_HAND",
                                                quantity=round(avg_demand * rng.uniform(2, 5), 0),
                                                batch_code=f"LOT-{item.sku}-{loc.code}",
                                                manufacture_date=today - timedelta(days=30),
                                                expiry_date=expiry))

        # A couple of items deliberately flagged as excess/obsolete for the demo
        if item.sku.endswith(("6", "2")) and rng.random() < 0.25:
            db.session.add(InventoryLedger(item_id=item.id, location_id=central.id, status="EXCESS",
                                            quantity=round(rng.uniform(1000, 3000), 0)))
        if item.sku.endswith("8") and rng.random() < 0.15:
            db.session.add(InventoryLedger(item_id=item.id, location_id=dc_west.id, status="OBSOLETE",
                                            quantity=round(rng.uniform(300, 900), 0)))

    db.session.flush()

    # --- Purchase orders, including the injected supplier delay ----------------
    po_num = 5000
    for item in items:
        supplier = item.primary_supplier
        for dest in [central, dc_north, dc_south, dc_west]:
            if rng.random() > 0.35:
                continue
            po_num += 1
            order_date = today - timedelta(days=rng.randint(1, 10))
            original_eta = order_date + timedelta(days=supplier.lead_time_mean_days)
            is_delayed = item in disrupted_items and supplier.id == disrupted_supplier.id and dest.code == "DC-SOUTH"
            current_eta = original_eta + timedelta(days=rng.randint(9, 14)) if is_delayed else original_eta
            po = PurchaseOrder(po_number=f"PO-{po_num}", supplier_id=supplier.id, destination_location_id=dest.id,
                                order_date=order_date, original_eta=original_eta, current_eta=current_eta,
                                status="DELAYED" if is_delayed else "OPEN", is_expedited=False)
            db.session.add(po)
            db.session.flush()
            qty = round(rng.uniform(500, 3000), 0)
            db.session.add(PurchaseOrderLine(po_id=po.id, item_id=item.id, quantity_ordered=qty,
                                              quantity_received=0, unit_cost=item.unit_cost))
            db.session.add(InventoryLedger(item_id=item.id, location_id=dest.id, status="ON_ORDER", quantity=qty))

    # --- A handful of open sales orders (drives ATP + allocation engine demo) --
    so_num = 9000
    for _ in range(40):
        item = rng.choice(items)
        customer = rng.choice(customers)
        loc = rng.choice(stores)
        so_num += 1
        so = SalesOrder(so_number=f"SO-{so_num}", customer_id=customer.id, location_id=loc.id,
                         order_date=today - timedelta(days=rng.randint(0, 5)),
                         requested_date=today + timedelta(days=rng.randint(1, 10)), status="OPEN")
        db.session.add(so)
        db.session.flush()
        qty = round(rng.uniform(50, 500), 0)
        db.session.add(SalesOrderLine(so_id=so.id, item_id=item.id, quantity_ordered=qty,
                                       quantity_allocated=round(qty * rng.uniform(0, 0.6), 0),
                                       unit_price=item.unit_price))

    db.session.commit()

    return {
        "organization": org.name, "suppliers": len(suppliers), "locations": 2 + len(regionals) + len(stores),
        "customers": len(customers), "items": len(items),
        "demand_history_rows": DemandHistory.query.count(),
        "ledger_rows": InventoryLedger.query.count(),
        "purchase_orders": PurchaseOrder.query.count(),
        "sales_orders": SalesOrder.query.count(),
        "disrupted_skus": [i.sku for i in disrupted_items],
        "disruption_supplier": disrupted_supplier.name,
    }
