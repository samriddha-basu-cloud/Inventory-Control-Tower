"""
End-to-end integration test: loads the synthetic demo network and exercises
every major page, the alert engine, optimization runs, scenarios, allocation,
replenishment and the REST API - i.e. the acceptance checklist in section 205.
"""
import json
import pytest
from app.services.demo_data_service import load_demo_data
from app.services import optimization_service, alert_service, scenario_service, replenishment_service


@pytest.fixture()
def demo_client(app, db):
    with app.app_context():
        load_demo_data(reset=True)
    return app.test_client()


def test_demo_data_loads(app, db):
    with app.app_context():
        summary = load_demo_data(reset=True)
    assert summary["items"] == 24
    assert summary["demand_history_rows"] > 0
    assert len(summary["disrupted_skus"]) == 3


@pytest.mark.parametrize("path", [
    "/dashboard", "/inventory/", "/inventory/abc-xyz", "/inventory/aging",
    "/network/", "/network/heatmap", "/optimization/", "/optimization/meio",
    "/optimization/rebalancing", "/exceptions/", "/exceptions/incidents",
    "/exceptions/recommendations", "/scenarios/", "/allocation/", "/replenishment/",
    "/reports/", "/master-data/", "/master-data/items", "/master-data/locations",
    "/master-data/suppliers", "/master-data/customers", "/master-data/orders",
])
def test_pages_render_200(demo_client, path):
    resp = demo_client.get(path)
    assert resp.status_code == 200, f"{path} returned {resp.status_code}"


def test_item_detail_page(demo_client):
    resp = demo_client.get("/inventory/SKU-1001")
    assert resp.status_code == 200


def test_safety_stock_recompute(app, demo_client):
    with app.app_context():
        result = optimization_service.recompute_safety_stock()
        assert result["updated"] > 0


def test_alert_engine_generates_alerts(app, demo_client):
    with app.app_context():
        optimization_service.recompute_safety_stock()
        result = alert_service.generate_alerts()
        assert result["alerts_generated"] > 0
        assert result["incidents_created"] == 1  # the injected supplier-delay incident


def test_meio_comparison_runs(app, demo_client):
    with app.app_context():
        result = optimization_service.run_meio_comparison("CENTRAL-DC", ["DC-NORTH", "DC-SOUTH", "DC-WEST"])
        assert result["status"] == "COMPLETED"
        assert len(result["sku_results"]) > 0
        for r in result["sku_results"]:
            assert "verdict" in r["comparison"]


def test_rebalancing_recommends_transfers(app, demo_client):
    with app.app_context():
        transfers = optimization_service.run_rebalancing(14)
        assert isinstance(transfers, list)


def test_scenario_run(app, demo_client):
    with app.app_context():
        scenario, results = scenario_service.run_scenario(
            "Test", {"demand_change_pct": 20, "lead_time_delta_days": 5})
        assert scenario.id is not None
        assert "delta" in results


def test_replenishment_recommendations(app, demo_client):
    with app.app_context():
        recs = replenishment_service.generate_replenishment_recommendations(persist=False)
        assert isinstance(recs, list)
        assert len(recs) > 0


def test_optimization_infeasible_is_reported_not_fabricated(app, demo_client):
    with app.app_context():
        result = optimization_service.run_meio_comparison("NOT-A-REAL-NODE", ["ALSO-FAKE"])
        assert result["status"] == "INFEASIBLE"
        assert "reason" in result


# --- API surface -----------------------------------------------------------

def test_api_inventory_position(demo_client):
    from app.models import Item, Location
    item = Item.query.first()
    loc = Location.query.filter_by(node_type="dc").first()
    resp = demo_client.get(f"/api/inventory/position?item_id={item.id}&location_id={loc.id}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "inventory_position" in data


def test_api_inventory_position_missing_params_returns_400(demo_client):
    resp = demo_client.get("/api/inventory/position")
    assert resp.status_code == 400


def test_api_alerts_list(demo_client):
    demo_client.post("/exceptions/generate")
    resp = demo_client.get("/api/alerts")
    assert resp.status_code == 200
    assert isinstance(resp.get_json(), list)


def test_api_optimization_run_safety_stock(demo_client):
    resp = demo_client.post("/api/optimization/run", json={"run_type": "safety_stock"})
    assert resp.status_code == 200
    assert resp.get_json()["updated"] >= 0


def test_api_scenario_run(demo_client):
    resp = demo_client.post("/api/scenario/run", json={"name": "api", "assumptions": {"demand_change_pct": 5}})
    assert resp.status_code == 200
    assert "scenario_id" in resp.get_json()


def test_excel_report_downloads(demo_client):
    resp = demo_client.get("/reports/excel")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def test_recommendation_approval_workflow(app, demo_client):
    with app.app_context():
        optimization_service.run_rebalancing(14)
        from app.models import Recommendation
        rec = Recommendation.query.filter_by(status="PENDING").first()
        assert rec is not None
        rec_id = rec.id

    resp = demo_client.post(f"/exceptions/recommendations/{rec_id}/decide",
                             data={"decision": "APPROVE", "reason": "test"})
    assert resp.status_code in (200, 302)

    with app.app_context():
        from app.models import Recommendation
        updated = Recommendation.query.get(rec_id)
        assert updated.status in ("APPROVED", "EXECUTED")
