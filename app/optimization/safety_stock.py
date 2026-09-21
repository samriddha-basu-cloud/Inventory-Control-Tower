"""
Safety Stock Engine — multiple documented methodologies (section 30-31).

Every function returns a dict with formula / inputs / assumptions / output so
the UI's Calculation Explorer can render the full derivation rather than a
bare number.
"""
import math
from app.analytics.service_level import z_for_service_level


def basic_statistical(std_dev_demand, service_level_pct, review_period_days=0):
    """SS = Z * sigma_D  (demand variability only, lead time treated as a
    constant already embedded in std_dev_demand, e.g. std dev over the
    protection period)."""
    z = z_for_service_level(service_level_pct)
    ss = z * std_dev_demand
    return {
        "method": "basic_statistical",
        "formula": "SS = Z * σ_D",
        "inputs": {"std_dev_demand": std_dev_demand, "service_level_pct": service_level_pct, "z": round(z, 4)},
        "assumptions": ["Demand during the protection period is approximately normal.",
                        "Lead time is constant / already reflected in σ_D."],
        "safety_stock": max(0.0, round(ss, 2)),
    }


def demand_variability(std_dev_demand_daily, lead_time_days, service_level_pct):
    """SS = Z * sigma_D * sqrt(LT)  — classic single-echelon formula assuming
    constant lead time and variable demand."""
    z = z_for_service_level(service_level_pct)
    ss = z * std_dev_demand_daily * math.sqrt(max(lead_time_days, 0))
    return {
        "method": "demand_variability",
        "formula": "SS = Z * σ_D * sqrt(LT)",
        "inputs": {
            "std_dev_demand_daily": std_dev_demand_daily,
            "lead_time_days": lead_time_days,
            "service_level_pct": service_level_pct,
            "z": round(z, 4),
        },
        "assumptions": ["Lead time is constant.", "Daily demand is i.i.d. and approximately normal."],
        "safety_stock": max(0.0, round(ss, 2)),
    }


def lead_time_variability(avg_demand_daily, std_dev_lead_time_days, service_level_pct):
    """SS = Z * D_avg * sigma_LT — demand constant, lead time variable."""
    z = z_for_service_level(service_level_pct)
    ss = z * avg_demand_daily * std_dev_lead_time_days
    return {
        "method": "lead_time_variability",
        "formula": "SS = Z * D̄ * σ_LT",
        "inputs": {
            "avg_demand_daily": avg_demand_daily,
            "std_dev_lead_time_days": std_dev_lead_time_days,
            "service_level_pct": service_level_pct,
            "z": round(z, 4),
        },
        "assumptions": ["Demand is constant.", "Lead time is approximately normal."],
        "safety_stock": max(0.0, round(ss, 2)),
    }


def combined_variability(avg_demand_daily, std_dev_demand_daily, lead_time_days,
                          std_dev_lead_time_days, service_level_pct):
    """
    SS = Z * sqrt( LT * σ_D^2 + D̄^2 * σ_LT^2 )

    The industry-standard combined demand + lead-time uncertainty formula.
    Both demand and lead time vary independently.
    """
    z = z_for_service_level(service_level_pct)
    variance = (lead_time_days * (std_dev_demand_daily ** 2)) + \
               ((avg_demand_daily ** 2) * (std_dev_lead_time_days ** 2))
    sigma_lt = math.sqrt(max(variance, 0))
    ss = z * sigma_lt
    return {
        "method": "combined_variability",
        "formula": "SS = Z * sqrt(LT*σ_D² + D̄²*σ_LT²)",
        "inputs": {
            "avg_demand_daily": avg_demand_daily,
            "std_dev_demand_daily": std_dev_demand_daily,
            "lead_time_days": lead_time_days,
            "std_dev_lead_time_days": std_dev_lead_time_days,
            "service_level_pct": service_level_pct,
            "z": round(z, 4),
            "sigma_lt": round(sigma_lt, 4),
        },
        "assumptions": ["Demand and lead time vary independently.",
                        "Both are approximately normally distributed."],
        "safety_stock": max(0.0, round(ss, 2)),
    }


def periodic_review(avg_demand_daily, std_dev_demand_daily, lead_time_days,
                     review_period_days, service_level_pct,
                     std_dev_lead_time_days=0.0):
    """
    Periodic-review safety stock protects the review period + lead time
    ("protection period"), since the next chance to react is one review
    cycle away.

    SS = Z * sqrt( (R+LT) * σ_D^2 + D̄^2 * σ_LT^2 )
    """
    z = z_for_service_level(service_level_pct)
    protection_period = review_period_days + lead_time_days
    variance = (protection_period * (std_dev_demand_daily ** 2)) + \
               ((avg_demand_daily ** 2) * (std_dev_lead_time_days ** 2))
    sigma = math.sqrt(max(variance, 0))
    ss = z * sigma
    return {
        "method": "periodic_review",
        "formula": "SS = Z * sqrt((R+LT)*σ_D² + D̄²*σ_LT²)",
        "inputs": {
            "avg_demand_daily": avg_demand_daily,
            "std_dev_demand_daily": std_dev_demand_daily,
            "lead_time_days": lead_time_days,
            "review_period_days": review_period_days,
            "protection_period_days": protection_period,
            "std_dev_lead_time_days": std_dev_lead_time_days,
            "service_level_pct": service_level_pct,
            "z": round(z, 4),
        },
        "assumptions": ["Stock is only reviewed every R days, so the buffer must cover R+LT."],
        "safety_stock": max(0.0, round(ss, 2)),
    }


def continuous_review(avg_demand_daily, std_dev_demand_daily, lead_time_days,
                       service_level_pct, std_dev_lead_time_days=0.0):
    """Continuous review = combined_variability with review period = 0
    (system can react the instant the reorder point is crossed)."""
    result = combined_variability(avg_demand_daily, std_dev_demand_daily,
                                   lead_time_days, std_dev_lead_time_days, service_level_pct)
    result["method"] = "continuous_review"
    return result


def seasonal(base_safety_stock, seasonal_index):
    """Scale a base safety stock by a seasonal index (e.g. 1.4 for peak month)."""
    ss = base_safety_stock * seasonal_index
    return {
        "method": "seasonal",
        "formula": "SS_seasonal = SS_base * SeasonalIndex",
        "inputs": {"base_safety_stock": base_safety_stock, "seasonal_index": seasonal_index},
        "assumptions": ["Seasonal index reflects the ratio of period demand to average demand."],
        "safety_stock": max(0.0, round(ss, 2)),
    }
