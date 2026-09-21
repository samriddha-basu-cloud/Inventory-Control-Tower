"""Reorder Point engine (section 33)."""


def reorder_point(avg_demand_daily, lead_time_days, safety_stock):
    """ROP = Expected Demand During Lead Time + Safety Stock."""
    lead_time_demand = avg_demand_daily * lead_time_days
    rop = lead_time_demand + safety_stock
    return {
        "formula": "ROP = (D̄ * LT) + SS",
        "inputs": {
            "avg_demand_daily": avg_demand_daily,
            "lead_time_days": lead_time_days,
            "safety_stock": safety_stock,
        },
        "lead_time_demand": round(lead_time_demand, 2),
        "reorder_point": round(rop, 2),
    }


def projected_stockout_date(current_stock, avg_demand_daily, as_of_date):
    """Days until projected stockout assuming flat average demand (no supply)."""
    import datetime
    if avg_demand_daily <= 0:
        return {"days_to_stockout": None, "stockout_date": None}
    days = current_stock / avg_demand_daily
    stockout_date = as_of_date + datetime.timedelta(days=days)
    return {
        "formula": "DaysToStockout = CurrentStock / D̄ (no incoming supply assumed)",
        "days_to_stockout": round(days, 1),
        "stockout_date": stockout_date.isoformat(),
    }
