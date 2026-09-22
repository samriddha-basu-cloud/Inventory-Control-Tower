"""Probabilistic lead-time intelligence.

Static master lead time is treated as a *claim*; observed history is the evidence. Statistics are computed
from LeadTimeObservation rows keyed (most specific first) by supplier+item+destination, supplier+item,
supplier+lane, supplier.  When enough observations exist a lognormal distribution is fitted; the empirical
percentiles P50/P75/P90/P95 are always reported.
"""
from __future__ import annotations

import math
from functools import lru_cache
from dataclasses import asdict, dataclass, field

import numpy as np
from scipy import stats as sps

from ..utils.stats import mean, median, percentile, std


@dataclass
class LTStats:
    n: int = 0
    static: float | None = None
    mean: float | None = None
    median: float | None = None
    std: float | None = None
    p50: float | None = None
    p75: float | None = None
    p90: float | None = None
    p95: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    eligible: bool = False           # enough observations to trust the distribution
    source: str = "static"           # observed | static | default
    dist: dict | None = None         # fitted distribution parameters
    reliability: float | None = None  # share of orders arriving within promised days (+tolerance)
    accuracy: float | None = None    # mean(observed - promised) / promised  (positive = late)

    def to_dict(self) -> dict:
        return asdict(self)


def compute_stats(observed: list[float], static: float | None, min_obs: int = 8,
                  promised: list[float] | None = None) -> LTStats:
    return _compute_cached(tuple(observed or ()), static, min_obs, tuple(promised or ()))


@lru_cache(maxsize=4096)
def _compute_cached(observed: tuple, static: float | None, min_obs: int, promised: tuple) -> LTStats:
    return _compute(list(observed), static, min_obs, list(promised) or None)


def _compute(observed: list[float], static: float | None, min_obs: int,
             promised: list[float] | None) -> LTStats:
    obs = [float(x) for x in observed if x is not None and x > 0]
    s = LTStats(n=len(obs), static=static)
    if not obs:
        s.source = "static" if static else "default"
        return s
    s.mean, s.median, s.std = mean(obs), median(obs), std(obs)
    s.p50, s.p75, s.p90, s.p95 = (percentile(obs, q) for q in (50, 75, 90, 95))
    s.minimum, s.maximum = min(obs), max(obs)
    s.eligible = len(obs) >= min_obs
    s.source = "observed" if s.eligible else ("static" if static else "observed")
    if s.eligible and s.std and s.std > 0:
        try:
            shape, loc, scale = sps.lognorm.fit(obs, floc=0)
            ks = sps.kstest(obs, "lognorm", args=(shape, loc, scale))
            s.dist = {"type": "lognormal", "sigma": float(shape), "mu": float(math.log(scale)),
                      "ks_stat": float(ks.statistic), "ks_p": float(ks.pvalue)}
        except Exception:  # pragma: no cover - fit failure falls back to empirical
            s.dist = None
    if promised:
        pairs = [(o, p) for o, p in zip(observed, promised) if o and p]
        if pairs:
            s.reliability = sum(1 for o, p in pairs if o <= p + 1) / len(pairs)
            s.accuracy = mean([(o - p) / p for o, p in pairs])
    return s


def planning_lead_time(stats: LTStats, basis: str, use_observed: bool, default_days: float,
                       assumed_cv: float = 0.0) -> tuple[float, float, str]:
    """Return (L, sigma_L, note) used by safety stock / ROP.

    basis: static | observed_mean | p50 | p90.  With P90 basis sigma_L is set to 0: the percentile already
    prices in the variability, so adding sigma_L on top would double count.
    """
    static = stats.static or default_days
    if not use_observed or basis == "static" or not stats.eligible:
        sigma = (stats.std or 0.0) if (stats.eligible and not use_observed) else 0.0
        if not stats.eligible and assumed_cv:
            sigma = static * assumed_cv
        note = "static master lead time" if stats.static else "default lead time (no master data!)"
        if use_observed and not stats.eligible and stats.n:
            note += f"; only {stats.n} observations (need more for a distribution)"
        return float(static), float(sigma), note
    if basis == "p90":
        return float(stats.p90), 0.0, f"observed P90 of {stats.n} orders (variability embedded in percentile)"
    if basis == "p50":
        return float(stats.p50), float(stats.std or 0.0), f"observed median of {stats.n} orders"
    return float(stats.mean), float(stats.std or 0.0), f"observed mean of {stats.n} orders"


def prob_arrival_by(day: float, eta_day: float, sigma_days: float) -> float:
    """P(shipment arrives no later than `day`), ETA uncertainty modelled as normal(eta, sigma)."""
    if sigma_days <= 1e-9:
        return 1.0 if day >= eta_day else 0.0
    return float(sps.norm.cdf((day - eta_day) / sigma_days))


def sample(stats: LTStats, n: int, rng: np.random.Generator, shift: float = 0.0, scale: float = 1.0) -> np.ndarray:
    """Draw lead times (days). Lognormal when fitted, else normal around the planning value, never < 1 day."""
    if stats.eligible and stats.dist:
        x = rng.lognormal(stats.dist["mu"], stats.dist["sigma"], n)
    elif stats.eligible and stats.mean:
        x = np.maximum(rng.normal(stats.mean, stats.std or 0.0, n), 1.0)
    else:
        base = stats.static or stats.mean or 7.0
        x = np.full(n, float(base))
    return np.maximum(1.0, x * scale + shift)
