"""Display formatting helpers (Jinja filters and API formatting)."""
from __future__ import annotations

import math
from datetime import date, datetime

from flask import current_app


def num(x, digits: int = 0) -> str:
    if x is None:
        return "–"
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    if math.isinf(x) or math.isnan(x):
        return "–"
    return f"{x:,.{digits}f}"


def pct(x, digits: int = 1, ratio: bool = True) -> str:
    if x is None:
        return "–"
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    if math.isinf(x) or math.isnan(x):
        return "–"
    return f"{x * 100 if ratio else x:,.{digits}f}%"


def money(x, compact: bool = True, digits: int = 1) -> str:
    """Currency with Indian lakh/crore compaction by default (config NUMBER_STYLE)."""
    if x is None:
        return "–"
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    if math.isinf(x) or math.isnan(x):
        return "–"
    sym = current_app.config.get("CURRENCY_SYMBOL", "₹") if current_app else "₹"
    style = current_app.config.get("NUMBER_STYLE", "IN") if current_app else "IN"
    sign = "-" if x < 0 else ""
    a = abs(x)
    if not compact:
        return f"{sign}{sym}{a:,.0f}"
    if style == "IN":
        if a >= 1e7:
            return f"{sign}{sym}{a / 1e7:,.{digits}f} Cr"
        if a >= 1e5:
            return f"{sign}{sym}{a / 1e5:,.{digits}f} L"
        if a >= 1e3:
            return f"{sign}{sym}{a / 1e3:,.{digits}f} K"
    else:
        if a >= 1e9:
            return f"{sign}{sym}{a / 1e9:,.{digits}f} B"
        if a >= 1e6:
            return f"{sign}{sym}{a / 1e6:,.{digits}f} M"
        if a >= 1e3:
            return f"{sign}{sym}{a / 1e3:,.{digits}f} K"
    return f"{sign}{sym}{a:,.0f}"


def dt(x, with_time: bool = False) -> str:
    if not x:
        return "–"
    if isinstance(x, datetime):
        return x.strftime("%d %b %Y %H:%M" if with_time else "%d %b %Y")
    if isinstance(x, date):
        return x.strftime("%d %b %Y")
    return str(x)


def days(x, digits: int = 1) -> str:
    if x is None or (isinstance(x, float) and (math.isinf(x) or math.isnan(x))):
        return "–"
    return f"{float(x):,.{digits}f} d"


def round_or_none(x, digits: int = 2):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return x
    if math.isinf(x) or math.isnan(x):
        return None
    return round(x, digits)


# Status vocabulary shared by every dashboard: never colour-only (icon + text + colour).
STATUS_META = {
    "NORMAL": ("Normal", "✓"),
    "WATCH": ("Watch", "◔"),
    "ATTENTION": ("Attention", "▲"),
    "CRITICAL": ("Critical", "✖"),
    "LOW": ("Low", "✓"),
    "MEDIUM": ("Medium", "◔"),
    "HIGH": ("High", "▲"),
}


def kpi_value(k: dict | None) -> str:
    """Format a KPI result dict by its unit."""
    if not k or k.get("value") is None:
        return "n/a"
    v, u = k["value"], k.get("unit")
    if u == "pct":
        return pct(v, 1)
    if u == "money":
        return money(v)
    if u == "days":
        return f"{v:,.1f} d"
    if u == "ratio":
        return f"{v:,.2f}"
    return num(v, 1)
