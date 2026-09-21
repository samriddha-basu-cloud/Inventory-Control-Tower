from app.analytics.working_capital import (
    days_of_supply, inventory_turns, days_inventory_outstanding, carrying_cost,
    working_capital_opportunity,
)


def test_days_of_supply_basic():
    assert days_of_supply(1000, 50) == 20.0


def test_days_of_supply_zero_demand():
    assert days_of_supply(1000, 0) is None


def test_inventory_turns():
    assert inventory_turns(1_200_000, 100_000) == 12.0


def test_inventory_turns_zero_inventory():
    assert inventory_turns(1_200_000, 0) is None


def test_dio_matches_turns_relationship():
    turns = inventory_turns(1_200_000, 100_000)
    dio = days_inventory_outstanding(100_000, 1_200_000)
    assert abs(dio - 365 / turns) < 0.5


def test_carrying_cost_percentage():
    result = carrying_cost(100_000, holding_cost_pct=0.22)
    assert result["annual_carrying_cost"] == 22_000.0


def test_carrying_cost_components_sum():
    components = {"capital": 0.10, "storage": 0.05, "obsolescence": 0.03}
    result = carrying_cost(100_000, components=components)
    assert abs(result["holding_cost_pct"] - 18.0) < 0.01


def test_working_capital_opportunity_no_excess():
    result = working_capital_opportunity(50_000, 60_000)
    assert result["potential_release"] == 0.0
