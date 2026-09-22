"""ABC / XYZ / FSN / HML / VED / SDE / criticality segmentation. All thresholds are configuration."""
from __future__ import annotations

from typing import Iterable

from ..utils.stats import cv as _cv, mean


def abc_classify(rows: list[dict], thresholds: dict | None = None) -> list[dict]:
    """rows: [{'key','annual_demand','unit_cost'}]. Annual Consumption Value = annual demand × unit cost.
    Class A = items making up the first `A` share of cumulative value, B up to `B`, remainder C.
    Cut-offs are cumulative-percent boundaries, e.g. {'A':0.80,'B':0.95} (A=top 80 %, B=next 15 %, C=rest)."""
    th = thresholds or {"A": 0.80, "B": 0.95}
    out = []
    for r in rows:
        acv = float(r.get("annual_demand") or 0) * float(r.get("unit_cost") or 0)
        out.append({**r, "annual_value": acv})
    out.sort(key=lambda r: -r["annual_value"])
    total = sum(r["annual_value"] for r in out) or 1.0
    cum = 0.0
    for r in out:
        before = cum
        cum += r["annual_value"]
        r["cum_pct"] = cum / total
        r["share_pct"] = r["annual_value"] / total
        # class by where the item STARTS on the cumulative curve, so the item that crosses a boundary stays in the higher class
        r["abc"] = "A" if before < th["A"] - 1e-12 else ("B" if before < th["B"] - 1e-12 else "C")
        if r["annual_value"] <= 0:
            r["abc"] = "C"
    return out


def xyz_classify(rows: list[dict], thresholds: dict | None = None) -> list[dict]:
    """rows: [{'key','series':[weekly demand...]}]. CV = σ / μ.  X: CV ≤ th['X'];  Y: CV ≤ th['Y'];  Z: above."""
    th = thresholds or {"X": 0.5, "Y": 1.0}
    out = []
    for r in rows:
        series = [float(x) for x in r.get("series", [])]
        m = mean(series)
        c = _cv(series) if series and m > 0 else float("inf")
        cls = "Z" if c == float("inf") else ("X" if c <= th["X"] else ("Y" if c <= th["Y"] else "Z"))
        out.append({**r, "mean": m, "cv": c, "xyz": cls})
    return out


def fsn_classify(annual_turns: float, days_since_movement: float | None, th: dict | None = None) -> str:
    """Fast / Slow / Non-moving by turns and recency of movement."""
    th = th or {"fast_turns": 6.0, "non_moving_days": 180}
    if days_since_movement is not None and days_since_movement > th["non_moving_days"]:
        return "N"
    if annual_turns >= th["fast_turns"]:
        return "F"
    return "S"


def hml_classify(unit_costs: dict[str, float], th: dict | None = None) -> dict[str, str]:
    """High / Medium / Low value by rank of unit value: top `H` share of items = H, next up to `M` = M, rest L."""
    th = th or {"H": 0.20, "M": 0.50}
    ranked = sorted(unit_costs.items(), key=lambda kv: -(kv[1] or 0))
    n = len(ranked) or 1
    out = {}
    for i, (k, _) in enumerate(ranked):
        share = (i + 1) / n
        out[k] = "H" if share <= th["H"] + 1e-12 else ("M" if share <= th["M"] + 1e-12 else "L")
    return out


def combine(*labels: str | None, sep: str = "-") -> str:
    return sep.join(l for l in labels if l)


def matrix(rows: Iterable[dict]) -> dict[str, dict[str, dict]]:
    """ABC×XYZ matrix: {'A': {'X': {'count':n,'value':v}, ...}, ...}."""
    m = {a: {x: {"count": 0, "value": 0.0} for x in "XYZ"} for a in "ABC"}
    for r in rows:
        a, x = r.get("abc"), r.get("xyz")
        if a in m and x in m[a]:
            m[a][x]["count"] += 1
            m[a][x]["value"] += r.get("annual_value", 0.0)
    return m
