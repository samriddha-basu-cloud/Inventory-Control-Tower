"""
Industry configuration engine (section 151).

Rather than fork the codebase per industry, each profile customizes
terminology, default KPIs shown first, default allocation policy for FEFO vs
FIFO, and which sample dataset "Launch Demo" loads. Everything else (models,
formulas, workflow) is shared.
"""

INDUSTRY_PROFILES = {
    "fmcg": {
        "label": "FMCG / Retail",
        "allocation_rule": "fifo",
        "emphasis_kpis": ["fill_rate", "dos", "excess_value", "inventory_turns"],
        "sample_dataset": "fmcg",
    },
    "pharma": {
        "label": "Pharma / Life Sciences",
        "allocation_rule": "fefo",
        "emphasis_kpis": ["expiry_exposure", "cycle_service_level", "quarantine_value", "otif"],
        "sample_dataset": "pharma",
    },
    "automotive": {
        "label": "Automotive",
        "allocation_rule": "priority_customer",
        "emphasis_kpis": ["line_risk", "component_shortage", "otif", "safety_stock_coverage"],
        "sample_dataset": "automotive",
    },
    "electronics": {
        "label": "High-Tech / Electronics",
        "allocation_rule": "margin",
        "emphasis_kpis": ["obsolescence_exposure", "excess_value", "last_time_buy_risk"],
        "sample_dataset": "electronics",
    },
    "spare_parts": {
        "label": "Spare Parts / MRO",
        "allocation_rule": "priority_customer",
        "emphasis_kpis": ["fill_rate", "criticality_coverage", "intermittent_demand_risk"],
        "sample_dataset": "spare_parts",
    },
}

DEFAULT_PROFILE = "fmcg"


def get_profile(key):
    return INDUSTRY_PROFILES.get(key, INDUSTRY_PROFILES[DEFAULT_PROFILE])
