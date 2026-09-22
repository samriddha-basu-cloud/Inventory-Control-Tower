"""Safety-stock methodologies and reorder point.

Notation (all per day, lead time in days):
    d̄  mean daily demand          σd  std-dev of daily demand
    L  mean lead time             σL  std-dev of lead time
    R  review period              z   normal quantile of the target cycle service level (CSL)
    σ_LTD = sqrt(L σd² + d̄² σL²)  -> std-dev of demand over the (uncertain) lead time

No single formula is "the" answer; the method is selectable per policy.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..utils.stats import z_from_loss, z_from_service_level

METHODS: dict[str, dict] = {
    "basic": {
        "name": "Basic variability (max–average)",
        "formula": "SS = d_max·L_max − d̄·L̄",
        "use": "Sparse history / no distribution assumptions; tends to over-protect.",
    },
    "demand": {
        "name": "Demand variability",
        "formula": "SS = z · σd · √L",
        "use": "Reliable suppliers (lead time treated as fixed).",
    },
    "leadtime": {
        "name": "Lead-time variability",
        "formula": "SS = z · d̄ · σL",
        "use": "Stable demand, unreliable supply.",
    },
    "combined": {
        "name": "Combined demand + lead-time variability",
        "formula": "SS = z · √(L·σd² + d̄²·σL²)",
        "use": "General purpose; assumes independent demand and lead time.",
    },
    "service_level": {
        "name": "Service-level (fill-rate, Type-2)",
        "formula": "SS = k·σ_LTD  where  G(k) = (1−β)·Q / σ_LTD   (G = normal loss function)",
        "use": "Target is the share of *units* filled from stock (β), not the chance of no stockout.",
    },
    "periodic": {
        "name": "Periodic review",
        "formula": "SS = z · √((L+R)·σd² + d̄²·σL²)",
        "use": "Fixed review cycle: protection interval is L + R.",
    },
    "continuous": {
        "name": "Continuous review (s,Q)",
        "formula": "SS = z · √(L·σd² + d̄²·σL²);  s = d̄·L + SS",
        "use": "Position monitored continuously; protection interval is L only.",
    },
    "empirical": {
        "name": "Empirical / bootstrap (intermittent demand)",
        "formula": "SS = P_CSL(lead-time demand sample) − mean(lead-time demand)",
        "use": "Automatically used for intermittent/lumpy demand where the normal approximation is invalid.",
    },
}


@dataclass
class SSResult:
    method: str
    ss: float
    z: float
    sigma_ltd: float
    formula: str
    inputs: dict
    warning: str | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def sigma_ltd(d_std: float, L: float, d_mean: float, L_std: float, R: float = 0.0) -> float:
    return math.sqrt(max(0.0, (L + R) * d_std ** 2 + (d_mean ** 2) * L_std ** 2))


def safety_stock(method: str, *, d_mean: float, d_std: float, L: float, L_std: float = 0.0, service_level: float = 0.95,
                 review_days: float = 0.0, d_max: float | None = None, L_max: float | None = None,
                 order_qty: float | None = None, ltd_samples=None) -> SSResult:
    if method not in METHODS:
        raise ValueError(f"Unknown safety stock method '{method}'. Choose one of {sorted(METHODS)}.")
    if not (0.5 <= service_level < 1.0):
        raise ValueError("service_level must be in [0.5, 1).")
    d_mean, d_std, L, L_std = max(d_mean, 0.0), max(d_std, 0.0), max(L, 0.0), max(L_std, 0.0)
    z = z_from_service_level(service_level)
    inputs = dict(d_mean=d_mean, d_std=d_std, L=L, L_std=L_std, service_level=service_level, z=round(z, 4),
                  review_days=review_days)
    warning = None
    if method == "basic":
        d_max = d_max if d_max is not None else d_mean + 2 * d_std
        L_max = L_max if L_max is not None else L + 2 * L_std
        ss = d_max * L_max - d_mean * L
        sig = sigma_ltd(d_std, L, d_mean, L_std)
        inputs.update(d_max=d_max, L_max=L_max)
    elif method == "demand":
        sig = d_std * math.sqrt(L)
        ss = z * sig
    elif method == "leadtime":
        sig = d_mean * L_std
        ss = z * sig
    elif method in ("combined", "continuous"):
        sig = sigma_ltd(d_std, L, d_mean, L_std)
        ss = z * sig
    elif method == "periodic":
        sig = sigma_ltd(d_std, L, d_mean, L_std, review_days)
        ss = z * sig
    elif method == "service_level":
        sig = sigma_ltd(d_std, L, d_mean, L_std)
        Q = order_qty if order_qty and order_qty > 0 else max(d_mean * max(L, 1), 1.0)
        if sig <= 1e-9:
            ss, z = 0.0, 0.0
        else:
            k = z_from_loss((1 - service_level) * Q / sig)
            ss, z = k * sig, k
        inputs.update(order_qty=Q, fill_rate_target=service_level)
    else:  # empirical
        import numpy as np
        xs = np.asarray(ltd_samples if ltd_samples is not None else [], float)
        if xs.size < 20:
            sig = sigma_ltd(d_std, L, d_mean, L_std)
            ss = z * sig
            warning = "Too few samples for empirical method; used combined formula."
        else:
            sig = float(xs.std())
            ss = float(np.quantile(xs, service_level) - xs.mean())
    ss = max(0.0, ss)
    return SSResult(method, ss, z, sig, METHODS[method]["formula"], inputs, warning)


def reorder_point(d_mean: float, L: float, ss: float) -> dict:
    """ROP = expected demand during lead time + safety stock."""
    ltd = max(d_mean, 0.0) * max(L, 0.0)
    return {"avg_daily_demand": d_mean, "lead_time": L, "lead_time_demand": ltd, "safety_stock": ss,
            "reorder_point": ltd + ss}
