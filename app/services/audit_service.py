"""Append-only audit trail + calculation lineage."""
from __future__ import annotations

from typing import Any

from flask import g, has_request_context

from ..extensions import db
from ..models import AuditLog


def current_actor() -> str:
    if has_request_context():
        return getattr(g, "user", None) and g.user.get("username") or "anonymous"
    return "system"


def log(category: str, entity_type: str, entity_id: Any, event: str, details: dict | None = None,
        trace: dict | None = None, actor: str | None = None) -> AuditLog:
    row = AuditLog(category=category, entity_type=entity_type, entity_id=str(entity_id), event=event,
                   details=_jsonable(details or {}), trace=_jsonable(trace) if trace else None,
                   actor=actor or current_actor())
    db.session.add(row)
    return row


def calc_trace(entity_type: str, entity_id: Any, formula: str, inputs: dict, result: dict,
               policy: dict | None = None, model: str = "ICT-engine v1") -> AuditLog:
    """Record input data, policy, formula, model and result for an important calculation."""
    return log("CALC", entity_type, entity_id, "calculation",
               {"formula": formula},
               {"inputs": inputs, "policy": policy or {}, "formula": formula, "model": model, "result": result})


def _jsonable(o):
    import datetime as _dt
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (_dt.date, _dt.datetime)):
        return o.isoformat()
    if isinstance(o, float) and (o != o or o in (float("inf"), float("-inf"))):
        return None
    try:
        import numpy as np
        if isinstance(o, np.generic):
            return o.item()
    except Exception:  # pragma: no cover
        pass
    return o
