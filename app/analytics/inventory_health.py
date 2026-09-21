"""
FIT Inventory Health Index (section 16).

Not an industry-standard universal score — a transparent, documented
composite (0-100) built from measurable network factors. "FIT" = the
platform's own scoring label, shown as such in the UI.

FIT = 100 - weighted penalty across:
    - stockout exposure   (25%)   share of SKU-locations below ROP
    - excess exposure      (20%)  share of inventory value classified excess
    - obsolescence exposure(15%)  share of inventory value obsolete/near-expiry
    - service level gap    (20%)  average (target - achieved) service level
    - inventory accuracy   (10%)  1 - abs variance rate
    - lead-time risk        (10%) average supplier lead-time CV, normalized

Weights are configurable; defaults shown are documented, not claimed
universal.
"""

DEFAULT_WEIGHTS = {
    "stockout_exposure": 0.25,
    "excess_exposure": 0.20,
    "obsolescence_exposure": 0.15,
    "service_level_gap": 0.20,
    "inventory_accuracy_gap": 0.10,
    "lead_time_risk": 0.10,
}


def fit_health_index(factors, weights=None):
    """
    factors: dict with keys matching DEFAULT_WEIGHTS, each a 0-1 "badness"
    fraction (0 = no problem, 1 = worst case).
    """
    weights = weights or DEFAULT_WEIGHTS
    penalty = 0.0
    breakdown = {}
    for key, weight in weights.items():
        badness = max(0.0, min(factors.get(key, 0.0), 1.0))
        contribution = badness * weight * 100
        penalty += contribution
        breakdown[key] = {
            "badness_fraction": round(badness, 3),
            "weight": weight,
            "penalty_points": round(contribution, 2),
        }
    score = max(0.0, round(100 - penalty, 1))
    if score >= 80:
        label = "Healthy"
    elif score >= 60:
        label = "Watch"
    else:
        label = "Critical"
    return {
        "score": score,
        "label": label,
        "breakdown": breakdown,
        "weights": weights,
        "methodology": "FIT Inventory Health Index (platform-defined composite, not an industry standard).",
    }
