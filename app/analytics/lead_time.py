"""Dynamic / probabilistic lead-time engine (sections 49-50)."""
import numpy as np


def lead_time_statistics(observed_lead_times_days):
    """observed_lead_times_days: list/array of historical actual lead times."""
    arr = np.array([x for x in observed_lead_times_days if x is not None], dtype=float)
    if arr.size == 0:
        return None
    return {
        "n_observations": int(arr.size),
        "mean": round(float(np.mean(arr)), 2),
        "median": round(float(np.median(arr)), 2),
        "std_dev": round(float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0, 2),
        "p50": round(float(np.percentile(arr, 50)), 2),
        "p80": round(float(np.percentile(arr, 80)), 2),
        "p90": round(float(np.percentile(arr, 90)), 2),
        "p95": round(float(np.percentile(arr, 95)), 2),
        "p99": round(float(np.percentile(arr, 99)), 2) if arr.size >= 5 else round(float(np.max(arr)), 2),
        "min": round(float(np.min(arr)), 2),
        "max": round(float(np.max(arr)), 2),
    }


def supplier_reliability_score(otif_pct, lead_time_cv):
    """
    Simple, documented reliability score (0-100):
        50% weight on OTIF, 50% weight on (1 - normalized lead-time CV).
    Not a universal industry standard - a transparent composite for ranking.
    """
    otif_component = max(0.0, min(otif_pct, 100.0))
    cv_component = max(0.0, 100.0 - min(lead_time_cv, 1.0) * 100.0)
    score = 0.5 * otif_component + 0.5 * cv_component
    return round(score, 1)


def lead_time_cv(mean_lead_time, std_dev_lead_time):
    if not mean_lead_time:
        return None
    return round(std_dev_lead_time / mean_lead_time, 3)
