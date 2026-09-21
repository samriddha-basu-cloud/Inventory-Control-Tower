"""Network-wide financial rollups: working capital, carrying cost, and the
FIT Inventory Health Index (sections 16, 27-29)."""
from app.extensions import db
from app.models import Item, Location, SafetyStockPolicy, InventoryLedger, Supplier
from app.services import inventory_service, risk_service, demand_stats
from app.analytics.working_capital import carrying_cost, inventory_turns, days_inventory_outstanding
from app.analytics.inventory_health import fit_health_index


def network_financials(holding_cost_pct=0.22, annualized_cogs=None):
    kpis = inventory_service.network_kpis()
    total_value = kpis["total_inventory_value"]
    cc = carrying_cost(total_value, holding_cost_pct)

    if annualized_cogs is None:
        # Approximate COGS from unit_cost * trailing annualized demand, network-wide.
        annualized_cogs = 0.0
        for item in Item.query.filter_by(is_active=True).all():
            for loc in Location.query.all():
                d = demand_stats.daily_demand_stats(item.id, loc.id)
                annualized_cogs += d["avg_demand_daily"] * 365 * item.unit_cost

    turns = inventory_turns(annualized_cogs, total_value) if total_value else None
    dio = days_inventory_outstanding(total_value, annualized_cogs) if annualized_cogs else None

    return {
        "total_inventory_value": total_value,
        "carrying_cost": cc,
        "annualized_cogs_estimate": round(annualized_cogs, 2),
        "inventory_turns": turns,
        "dio_days": dio,
        "excess_value": kpis["excess_value"],
        "obsolete_value": kpis["obsolete_value"],
    }


def network_health_index():
    combos = db.session.query(SafetyStockPolicy.item_id, SafetyStockPolicy.location_id).all()
    kpis = inventory_service.network_kpis()
    total_value = max(kpis["total_inventory_value"], 1)

    stockout_flags = 0
    service_gaps = []
    lead_time_cvs = []
    n = max(len(combos), 1)
    for item_id, location_id in combos:
        risk = risk_service.stockout_risk(item_id, location_id)
        if risk["probability_pct"] >= 30:
            stockout_flags += 1
        item = Item.query.get(item_id)
        if item and item.primary_supplier and item.primary_supplier.lead_time_mean_days:
            lead_time_cvs.append(
                (item.primary_supplier.lead_time_std_days or 0) / item.primary_supplier.lead_time_mean_days
            )

    avg_lt_cv = sum(lead_time_cvs) / len(lead_time_cvs) if lead_time_cvs else 0.0

    factors = {
        "stockout_exposure": stockout_flags / n,
        "excess_exposure": kpis["excess_value"] / total_value,
        "obsolescence_exposure": kpis["obsolete_value"] / total_value,
        "service_level_gap": min(stockout_flags / n * 1.2, 1.0),  # proxy: correlated with stockout exposure
        "inventory_accuracy_gap": 0.05,  # no cycle-count variance data loaded yet -> conservative constant
        "lead_time_risk": min(avg_lt_cv, 1.0),
    }
    return fit_health_index(factors)


def supplier_risk_heatmap():
    return [{"supplier": s, "risk": risk_service.supplier_risk_score(s)} for s in Supplier.query.all()]
