"""JSON helpers: dates, numpy scalars, NaN/inf-safe serialisation for DB JSON columns and API responses."""
import datetime as _dt
import json
import math
from decimal import Decimal

from flask.json.provider import DefaultJSONProvider


def _default(o):
    if isinstance(o, (_dt.datetime, _dt.date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    try:
        import numpy as np
        if isinstance(o, np.generic):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
    except Exception:  # pragma: no cover
        pass
    if hasattr(o, "to_dict"):
        return o.to_dict()
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")


def _clean(o):
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    if isinstance(o, dict):
        return {k if isinstance(k, (str, int, float, bool)) or k is None else str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def dumps(obj, **kw):
    kw.setdefault("default", _default)
    return json.dumps(_clean(obj), **kw)


class ICTJSONProvider(DefaultJSONProvider):
    def dumps(self, obj, **kwargs):
        kwargs.setdefault("default", _default)
        return json.dumps(_clean(obj), **{k: v for k, v in kwargs.items() if k in ("default", "indent", "separators", "sort_keys", "ensure_ascii")})
