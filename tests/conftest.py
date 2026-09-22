import pytest

from app import create_app
from app.extensions import db


@pytest.fixture(scope="session")
def app():
    """One app + one demo dataset (3 industries) shared by read-mostly tests: loading takes a few seconds."""
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        from app.services.seed_service import seed_reference_data
        seed_reference_data(create_users=False)
        from app.services import demo_service
        demo_service.load_demo(["AUTOMOTIVE", "FMCG", "PHARMA"], seed=42)
    return app


@pytest.fixture()
def ctx(app):
    with app.app_context():
        yield


@pytest.fixture()
def client(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["_csrf"] = "tok"
    return c


def post(client, url, data=None, **kw):
    d = dict(data or {})
    d["csrf_token"] = "tok"
    return client.post(url, data=d, follow_redirects=kw.pop("follow_redirects", True), **kw)


@pytest.fixture()
def tiny():
    """Isolated empty app+db for ledger / reconciliation unit tests."""
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        from app.services.seed_service import seed_reference_data
        seed_reference_data(create_users=False)
        from app.models import Item, Location
        it = Item(sku="T-1", description="Test item", uom="EA", unit_cost=10, selling_price=15, moq=10, order_multiple=5)
        lot_it = Item(sku="T-LOT", description="Lot item", uom="EA", unit_cost=10, lot_tracked=True, shelf_life_days=100)
        a, b = Location(code="A", name="Node A", loc_type="CDC", lat=19.0, lon=73.0), Location(code="B", name="Node B", loc_type="RDC", lat=18.5, lon=73.8)
        db.session.add_all([it, lot_it, a, b])
        db.session.commit()
    return app
