"""Hierarchical policy resolution.

Precedence (most specific wins):

    GLOBAL  <  INDUSTRY  <  REGION  <  NODE  <  CATEGORY  <  SKU  <  SKU_LOCATION

Params are merged key-by-key from broad to specific, so a SKU policy that only sets `service_level`
keeps the `method` defined at INDUSTRY level. `resolve()` returns the merged params AND the ordered
list of policies that contributed (for audit / explainability).
"""
from __future__ import annotations

from typing import Iterable

LEVELS = ["GLOBAL", "INDUSTRY", "REGION", "NODE", "CATEGORY", "SKU", "SKU_LOCATION"]
_RANK = {lv: i for i, lv in enumerate(LEVELS)}


def scope_keys(item: dict, loc: dict) -> dict[str, list[str]]:
    """Which scope_key values apply to an (item, location) pair at each level."""
    return {
        "GLOBAL": ["*"],
        "INDUSTRY": [x for x in [item.get("industry"), loc.get("industry")] if x],
        "REGION": [x for x in [loc.get("region")] if x],
        "NODE": [x for x in [loc.get("code")] if x],
        "CATEGORY": [x for x in [item.get("category"), item.get("family_code")] if x],
        "SKU": [x for x in [item.get("sku")] if x],
        "SKU_LOCATION": [f"{item.get('sku')}|{loc.get('code')}"],
    }


def applicable(policies: Iterable, item: dict, loc: dict) -> list:
    keys = scope_keys(item, loc)
    out = []
    for p in policies:
        if not getattr(p, "active", True):
            continue
        if p.scope_key in keys.get(p.scope_level, []):
            out.append(p)
    out.sort(key=lambda p: (_RANK.get(p.scope_level, 0), getattr(p, "id", 0)))
    return out


def resolve(policies: Iterable, item: dict, loc: dict) -> tuple[dict, list[dict]]:
    merged: dict = {}
    trail: list[dict] = []
    for p in applicable(policies, item, loc):
        merged.update({k: v for k, v in (p.params or {}).items() if v is not None})
        trail.append({"id": p.id, "level": p.scope_level, "key": p.scope_key, "params": p.params})
    return merged, trail


class PolicyIndex:
    """Pre-indexed policies for fast resolution across hundreds of item-location pairs."""

    def __init__(self, policies: Iterable):
        self._idx: dict[tuple[str, str], list] = {}
        for p in policies:
            if getattr(p, "active", True):
                self._idx.setdefault((p.scope_level, p.scope_key), []).append(p)

    def resolve(self, item: dict, loc: dict) -> tuple[dict, list[dict]]:
        keys = scope_keys(item, loc)
        merged: dict = {}
        trail: list[dict] = []
        for level in LEVELS:
            for k in keys[level]:
                for p in sorted(self._idx.get((level, k), []), key=lambda p: p.id):
                    merged.update({kk: v for kk, v in (p.params or {}).items() if v is not None})
                    trail.append({"id": p.id, "level": level, "key": k, "params": p.params})
        return merged, trail
