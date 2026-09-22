from __future__ import annotations

from datetime import date, datetime, timezone

from ..extensions import db


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CanonicalMixin:
    """Every major canonical entity carries lineage + effectivity so multi-source data can be harmonised."""
    source_system = db.Column(db.String(40), default="ICT", nullable=False)
    source_id = db.Column(db.String(80))
    last_updated = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)
    effective_date = db.Column(db.Date, default=date.today)
    status = db.Column(db.String(24), default="ACTIVE", nullable=False)


def to_dict(obj, exclude: tuple = ()) -> dict:
    out = {}
    for c in obj.__table__.columns:
        if c.name in exclude:
            continue
        v = getattr(obj, c.name)
        if isinstance(v, (datetime, date)):
            v = v.isoformat()
        out[c.name] = v
    return out
