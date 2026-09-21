"""Stockout prediction, excess/obsolescence detection, supplier risk (sections
52-56)."""
from datetime import date
from scipy.stats import norm

from app.models import Item, Location, SafetyStockPolicy, Supplier
from app.services import inventory_service, demand_stats
from app.optimization.reorder_point import projected_stockout_date


def stockout_risk(item_id, location_id, lead_time_days=None):
    demand = demand_stats.daily_demand_stats(item_id, location_id)
    avail = inventory_service.available_quantity(item_id, location_id)
    policy = SafetyStockPolicy.query.filter_by(item_id=item_id, location_id=location_id).first()
    item = Item.query.get(item_id)
    lt = lead_time_days or (item.primary_supplier.lead_time_mean_days if item and item.primary_supplier else 14)
    lt_std = item.primary_supplier.lead_time_std_days if item and item.primary_supplier else 2

    if demand["avg_demand_daily"] <= 0:
        return {"probability_pct": 0.0, "days_to_stockout": None, "stockout_date": None,
                "demand_at_risk": 0.0, "revenue_at_risk": 0.0}

    lead_time_demand_mean = demand["avg_demand_daily"] * lt
    # variance of demand during lead time (demand variability + lead-time variability)
    variance = lt * (demand["std_dev_demand_daily"] ** 2) + (demand["avg_demand_daily"] ** 2) * (lt_std ** 2)
    sigma = variance ** 0.5

    if sigma <= 0:
        probability = 100.0 if avail < lead_time_demand_mean else 0.0
    else:
        z = (avail - lead_time_demand_mean) / sigma
        probability = round((1 - norm.cdf(z)) * 100, 1)

    proj = projected_stockout_date(avail, demand["avg_demand_daily"], date.today())
    shortfall = max(0.0, lead_time_demand_mean - avail)
    revenue_at_risk = shortfall * (item.unit_price if item else 0.0)

    return {
        "probability_pct": probability,
        "days_to_stockout": proj["days_to_stockout"],
        "stockout_date": proj["stockout_date"],
        "lead_time_demand_mean": round(lead_time_demand_mean, 1),
        "available": round(avail, 1),
        "safety_stock": policy.effective_safety_stock if policy else None,
        "demand_at_risk": round(shortfall, 1),
        "revenue_at_risk": round(revenue_at_risk, 2),
    }


def excess_detection(item_id, location_id, target_dos_days=30, excess_multiple=2.0):
    """
    Flags excess only when on-hand meaningfully exceeds a documented target
    (safety stock + lead-time demand) by `excess_multiple`, not merely
    because it exceeds safety stock (section 55).
    """
    demand = demand_stats.daily_demand_stats(item_id, location_id)
    avail = inventory_service.available_quantity(item_id, location_id)
    policy = SafetyStockPolicy.query.filter_by(item_id=item_id, location_id=location_id).first()
    ss = policy.effective_safety_stock if policy else 0.0
    item = Item.query.get(item_id)
    lt = item.primary_supplier.lead_time_mean_days if item and item.primary_supplier else 14

    target_stock = ss + demand["avg_demand_daily"] * lt
    excess_threshold = target_stock * excess_multiple
    is_excess = avail > excess_threshold and avail > 0
    excess_qty = max(0.0, avail - target_stock) if is_excess else 0.0
    excess_value = excess_qty * (item.unit_cost if item else 0.0)
    dos = (avail / demand["avg_demand_daily"]) if demand["avg_demand_daily"] > 0 else None

    return {
        "is_excess": is_excess,
        "available": round(avail, 1),
        "target_stock": round(target_stock, 1),
        "excess_quantity": round(excess_qty, 1),
        "excess_value": round(excess_value, 2),
        "days_of_supply": round(dos, 1) if dos is not None else None,
        "days_since_last_txn": demand["days_since_last_txn"],
        "classification": _movement_classification(demand, dos),
    }


def _movement_classification(demand_stats_row, dos):
    days_since = demand_stats_row["days_since_last_txn"]
    if days_since is None:
        return "NO_DEMAND_HISTORY"
    if days_since > 180:
        return "OBSOLETE_CANDIDATE"
    if days_since > 90:
        return "NON_MOVING"
    if dos is not None and dos > 90:
        return "SLOW_MOVING"
    return "ACTIVE"


def supplier_risk_score(supplier: Supplier):
    """
    Composite 0-100 supplier risk (documented, not a universal standard):
    higher = riskier. Weighted: OTIF gap 40%, lead-time CV 30%, single-source
    flag 20%, defect rate 10%.
    """
    otif_gap = max(0.0, 100 - (supplier.otif_pct or 85))
    cv = (supplier.lead_time_std_days / supplier.lead_time_mean_days) if supplier.lead_time_mean_days else 0
    cv_component = min(cv, 1.0) * 100
    single_source_component = 100 if supplier.single_source_risk else 0
    defect_component = min(supplier.defect_rate_pct or 0, 100)

    score = (0.40 * otif_gap) + (0.30 * cv_component) + (0.20 * single_source_component) + (0.10 * defect_component)
    if score >= 60:
        band = "HIGH"
    elif score >= 30:
        band = "MEDIUM"
    else:
        band = "LOW"
    return {"score": round(score, 1), "band": band,
            "components": {"otif_gap": round(otif_gap, 1), "lead_time_cv": round(cv, 2),
                            "single_source": supplier.single_source_risk,
                            "defect_rate_pct": supplier.defect_rate_pct}}
