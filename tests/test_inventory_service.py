from app.models import Item, Location, InventoryLedger, SalesOrder, SalesOrderLine, Customer
from app.services import inventory_service


def _seed_item_location(db):
    item = Item(sku="TEST-1", name="Test Item", unit_cost=10, unit_price=20, moq=100, order_multiple=10)
    loc = Location(code="LOC-1", name="Test Location", node_type="dc")
    db.session.add_all([item, loc])
    db.session.commit()
    return item, loc


def test_status_quantities_empty(app, db):
    item, loc = _seed_item_location(db)
    qtys = inventory_service.status_quantities(item.id, loc.id)
    assert qtys == {}


def test_available_quantity_subtracts_encumbered(app, db):
    item, loc = _seed_item_location(db)
    db.session.add_all([
        InventoryLedger(item_id=item.id, location_id=loc.id, status="ON_HAND", quantity=100),
        InventoryLedger(item_id=item.id, location_id=loc.id, status="ALLOCATED", quantity=30),
        InventoryLedger(item_id=item.id, location_id=loc.id, status="QUARANTINED", quantity=10),
    ])
    db.session.commit()
    assert inventory_service.available_quantity(item.id, loc.id) == 60


def test_available_quantity_never_negative(app, db):
    item, loc = _seed_item_location(db)
    db.session.add_all([
        InventoryLedger(item_id=item.id, location_id=loc.id, status="ON_HAND", quantity=10),
        InventoryLedger(item_id=item.id, location_id=loc.id, status="ALLOCATED", quantity=50),
    ])
    db.session.commit()
    assert inventory_service.available_quantity(item.id, loc.id) == 0


def test_inventory_position_formula(app, db):
    item, loc = _seed_item_location(db)
    db.session.add_all([
        InventoryLedger(item_id=item.id, location_id=loc.id, status="ON_HAND", quantity=100),
        InventoryLedger(item_id=item.id, location_id=loc.id, status="ON_ORDER", quantity=50),
        InventoryLedger(item_id=item.id, location_id=loc.id, status="IN_TRANSIT", quantity=20),
        InventoryLedger(item_id=item.id, location_id=loc.id, status="ALLOCATED", quantity=15),
    ])
    db.session.commit()
    pos = inventory_service.inventory_position(item.id, loc.id)
    # 100 + 50 + 20 - 15 - 0(backorders) = 155
    assert pos["inventory_position"] == 155


def test_atp_accounts_for_open_demand(app, db):
    item, loc = _seed_item_location(db)
    customer = Customer(code="CUST-T", name="Test Customer")
    db.session.add_all([
        InventoryLedger(item_id=item.id, location_id=loc.id, status="ON_HAND", quantity=100),
        customer,
    ])
    db.session.commit()
    so = SalesOrder(so_number="SO-T1", customer_id=customer.id, location_id=loc.id)
    db.session.add(so)
    db.session.commit()
    db.session.add(SalesOrderLine(so_id=so.id, item_id=item.id, quantity_ordered=40, quantity_allocated=0))
    db.session.commit()

    atp = inventory_service.atp(item.id, loc.id)
    assert atp["open_demand"] == 40
    assert atp["atp"] == 60  # 100 available - 40 open demand


def test_network_kpis_no_data(app, db):
    kpis = inventory_service.network_kpis()
    assert kpis["on_hand_units"] == 0.0
    assert kpis["total_inventory_value"] == 0.0
