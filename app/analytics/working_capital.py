"""
Financial inventory metrics — DOS, DIO, turns, carrying cost, working
capital (sections 25-29).
"""


def days_of_supply(available_inventory, avg_daily_demand):
    """DOS = Available Inventory / Average Daily Demand."""
    if avg_daily_demand <= 0:
        return None
    return round(available_inventory / avg_daily_demand, 1)


def inventory_turns(annualized_cogs, average_inventory_value):
    if not average_inventory_value:
        return None
    return round(annualized_cogs / average_inventory_value, 2)


def days_inventory_outstanding(average_inventory_value, annualized_cogs, days_in_year=365):
    if not annualized_cogs:
        return None
    return round((average_inventory_value / annualized_cogs) * days_in_year, 1)


def carrying_cost(average_inventory_value, holding_cost_pct=0.22, components=None):
    """
    Either pass an overall `holding_cost_pct` (annual % of inventory value),
    or a `components` dict of {capital, storage, insurance, handling,
    obsolescence, shrinkage} each as a fraction, which are summed.
    """
    if components:
        pct = sum(components.values())
    else:
        pct = holding_cost_pct
    return {
        "holding_cost_pct": round(pct * 100, 2),
        "annual_carrying_cost": round(average_inventory_value * pct, 2),
        "components": components,
    }


def working_capital_opportunity(current_inventory_value, target_inventory_value, holding_cost_pct=0.22):
    excess_value = max(0.0, current_inventory_value - target_inventory_value)
    return {
        "current_inventory_value": round(current_inventory_value, 2),
        "target_inventory_value": round(target_inventory_value, 2),
        "potential_release": round(excess_value, 2),
        "annual_carrying_cost_saved": round(excess_value * holding_cost_pct, 2),
    }
