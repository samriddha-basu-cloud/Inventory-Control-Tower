"""Small, dependency-light statistics helpers used across the engines."""
from __future__ import annotations

import math
from typing import Iterable, Sequence

import numpy as np
from scipy.stats import norm


def as_list(xs: Iterable[float]) -> list[float]:
    return [float(x) for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]


def mean(xs: Iterable[float]) -> float:
    xs = as_list(xs)
    return float(sum(xs) / len(xs)) if xs else 0.0


def std(xs: Iterable[float], ddof: int = 1) -> float:
    xs = as_list(xs)
    if len(xs) <= ddof:
        return 0.0
    return float(np.std(xs, ddof=ddof))


def cv(xs: Iterable[float]) -> float:
    xs = as_list(xs)
    m = mean(xs)
    if m == 0:
        return float("inf") if any(xs) else 0.0
    return std(xs) / abs(m)


def percentile(xs: Sequence[float], q: float) -> float:
    xs = as_list(xs)
    return float(np.percentile(xs, q)) if xs else 0.0


def median(xs: Iterable[float]) -> float:
    return percentile(list(xs), 50)


def z_from_service_level(sl: float) -> float:
    """Z-score for a cycle service level (Type-1). Clamped to a sane open interval."""
    sl = min(max(float(sl), 0.5), 0.9999)
    return float(norm.ppf(sl))


def service_level_from_z(z: float) -> float:
    return float(norm.cdf(z))


def normal_loss(z: float) -> float:
    """Standard normal loss function G(z) = phi(z) - z (1 - Phi(z)) = E[(Z - z)+]."""
    return float(norm.pdf(z) - z * (1.0 - norm.cdf(z)))


def z_from_loss(target_loss: float) -> float:
    """Invert G(z) = target_loss by bisection (G is strictly decreasing)."""
    if target_loss <= 0:
        return 4.0
    lo, hi = -4.0, 6.0
    if target_loss >= normal_loss(lo):
        return lo
    for _ in range(80):
        mid = (lo + hi) / 2
        if normal_loss(mid) > target_loss:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def wape(actual: Sequence[float], forecast: Sequence[float]) -> float:
    a, f = np.asarray(actual, float), np.asarray(forecast, float)
    denom = np.abs(a).sum()
    return float(np.abs(a - f).sum() / denom) if denom else 0.0


def bias(actual: Sequence[float], forecast: Sequence[float]) -> float:
    """Forecast bias = sum(forecast - actual) / sum(actual). Positive = over-forecast."""
    a, f = np.asarray(actual, float), np.asarray(forecast, float)
    denom = a.sum()
    return float((f - a).sum() / denom) if denom else 0.0


def adi_cv2(weekly: Sequence[float]) -> tuple[float, float]:
    """Syntetos-Boylan demand classification inputs: average demand interval and CV^2 of non-zero demand."""
    xs = as_list(weekly)
    nz = [x for x in xs if x > 0]
    if not nz:
        return float("inf"), 0.0
    adi = len(xs) / len(nz)
    m = mean(nz)
    cv2 = (std(nz) / m) ** 2 if m else 0.0
    return adi, cv2


def demand_profile(weekly: Sequence[float]) -> str:
    """SMOOTH / ERRATIC / INTERMITTENT / LUMPY (Syntetos-Boylan cut-offs 1.32 and 0.49)."""
    if len(as_list(weekly)) < 8:
        return "UNKNOWN"
    adi, cv2 = adi_cv2(weekly)
    if adi < 1.32:
        return "SMOOTH" if cv2 < 0.49 else "ERRATIC"
    return "INTERMITTENT" if cv2 < 0.49 else "LUMPY"


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default
