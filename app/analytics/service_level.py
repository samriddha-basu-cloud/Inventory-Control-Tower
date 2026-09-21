"""
Service-level engine.

IMPORTANT: Cycle Service Level (probability of NOT stocking out during a
replenishment cycle) is not the same thing as Fill Rate (% of demand met
directly from stock). Conflating the two silently overstates achieved
service in most real networks with non-trivial variability.
"""
from scipy.stats import norm


def z_for_service_level(service_level_pct):
    """Convert a target cycle service level (e.g. 95) to a standard-normal Z."""
    if service_level_pct is None:
        raise ValueError("service_level_pct is required")
    p = service_level_pct / 100.0
    if not (0 < p < 1):
        raise ValueError("service_level_pct must be strictly between 0 and 100")
    return float(norm.ppf(p))


def cycle_service_level_for_z(z):
    return float(norm.cdf(z)) * 100.0


def fill_rate_from_ss(std_dev_demand_lt, safety_stock):
    """
    Approximate expected fill rate given safety stock, using the standard
    normal loss function E[max(0, D-SS)] to estimate expected shortage per
    cycle. This is an approximation (assumes demand during lead time is
    normally distributed) — documented, not claimed exact.
    """
    if std_dev_demand_lt <= 0:
        return 100.0
    k = safety_stock / std_dev_demand_lt
    # standard normal loss function L(k) = phi(k) - k * (1 - Phi(k))
    from scipy.stats import norm as _norm
    loss = _norm.pdf(k) - k * (1 - _norm.cdf(k))
    expected_shortage = std_dev_demand_lt * loss
    return {
        "method": "normal_loss_function_approximation",
        "formula": "FillRate ≈ 1 - E[shortage]/σ_LT, where E[shortage]=σ_LT * L(k), k=SS/σ_LT",
        "inputs": {"std_dev_demand_lt": std_dev_demand_lt, "safety_stock": safety_stock},
        "expected_shortage_per_cycle": round(expected_shortage, 3),
    }


ITEM_FILL_RATE_TARGETS_BY_VED = {
    "V": 99.5,  # Vital
    "E": 97.0,  # Essential
    "D": 90.0,  # Desirable
}
