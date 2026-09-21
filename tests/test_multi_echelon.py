from app.optimization.single_echelon import optimize_single_echelon
from app.optimization.multi_echelon import optimize_two_echelon, compare_single_vs_multi_echelon


def test_single_echelon_sums_node_safety_stock():
    nodes = [
        {"node": "A", "avg_demand_daily": 20, "std_dev_demand_daily": 5, "lead_time_days": 10,
         "std_dev_lead_time_days": 2, "service_level_pct": 95, "unit_cost": 10},
        {"node": "B", "avg_demand_daily": 15, "std_dev_demand_daily": 4, "lead_time_days": 10,
         "std_dev_lead_time_days": 2, "service_level_pct": 95, "unit_cost": 10},
    ]
    result = optimize_single_echelon(nodes)
    assert result["total_safety_stock_units"] == round(sum(n["safety_stock"] for n in result["nodes"]), 2)
    assert len(result["nodes"]) == 2


def test_two_echelon_demand_pooling_reduces_downstream_variance_exposure():
    downstream = [
        {"node": "D1", "avg_demand_daily": 20, "std_dev_demand_daily": 8,
         "internal_lead_time_days": 2, "internal_std_dev_lead_time_days": 0.5, "service_level_pct": 95, "unit_cost": 10},
        {"node": "D2", "avg_demand_daily": 20, "std_dev_demand_daily": 8,
         "internal_lead_time_days": 2, "internal_std_dev_lead_time_days": 0.5, "service_level_pct": 95, "unit_cost": 10},
    ]
    upstream = {"node": "HUB", "external_lead_time_days": 14, "external_std_dev_lead_time_days": 0.5,
                "service_level_pct": 95, "unit_cost": 10}
    result = optimize_two_echelon(upstream, downstream)
    # pooled std dev should be less than the sum of individual std devs (risk pooling)
    assert result["upstream"]["pooled_std_dev_demand"] < sum(d["std_dev_demand_daily"] for d in downstream)
    assert result["total_safety_stock_units"] > 0


def test_compare_reports_direction_honestly():
    single = {"total_safety_stock_units": 100, "total_safety_stock_value": 1000}
    multi_lower = {"total_safety_stock_units": 60, "total_safety_stock_value": 600}
    multi_higher = {"total_safety_stock_units": 140, "total_safety_stock_value": 1400}

    comp_win = compare_single_vs_multi_echelon(single, multi_lower)
    assert comp_win["working_capital_release"] == 400
    assert "releases working capital" in comp_win["verdict"]

    comp_lose = compare_single_vs_multi_echelon(single, multi_higher)
    assert comp_lose["working_capital_release"] == -400
    assert "MORE network safety stock" in comp_lose["verdict"]
