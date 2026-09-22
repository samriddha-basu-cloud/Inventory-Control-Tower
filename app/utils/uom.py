"""Unit-of-measure conversion that refuses invalid conversions.

Two mechanisms:
  1. Global conversions inside one physical *dimension* (mass, volume, length).
  2. Item-specific pack conversions (EA <-> BOX <-> CASE <-> PALLET) registered per item.
Converting across dimensions (e.g. KG -> LITRE) or between packs with no registered factor raises
UomConversionError. Nothing is ever guessed.
"""
from __future__ import annotations


class UomConversionError(ValueError):
    pass


# Dimension -> {unit: factor to the dimension's base unit}
DIMENSIONS = {
    "count": {"EA": 1.0, "PC": 1.0, "DOZ": 12.0},
    "mass": {"KG": 1.0, "G": 0.001, "MT": 1000.0, "LB": 0.45359237},
    "volume": {"LITRE": 1.0, "L": 1.0, "ML": 0.001, "M3": 1000.0},
    "length": {"METER": 1.0, "M": 1.0, "CM": 0.01, "MM": 0.001, "KM": 1000.0},
}
# Pack units carry no intrinsic factor: they need an item-level conversion.
PACK_UNITS = {"BOX", "CASE", "PALLET", "TOTE", "DRUM", "BAG", "ROLL", "SET"}
KNOWN_UNITS = {u for d in DIMENSIONS.values() for u in d} | PACK_UNITS


def norm(u: str | None) -> str:
    return (u or "EA").strip().upper()


def dimension_of(u: str) -> str | None:
    u = norm(u)
    for dim, table in DIMENSIONS.items():
        if u in table:
            return dim
    return None


def is_valid_uom(u: str | None) -> bool:
    return norm(u) in KNOWN_UNITS


def convert(qty: float, from_uom: str, to_uom: str, item_factors: dict | None = None) -> float:
    """Convert qty between units.

    item_factors: {(from, to): factor} meaning 1 `from` = factor `to` (e.g. {("CASE","EA"): 24}).
    Path search runs over item-level factors (both directions) and same-dimension global factors, so
    PALLET -> CASE -> EA -> DOZ chains work, while KG -> LITRE never does.
    """
    f, t = norm(from_uom), norm(to_uom)
    if f == t:
        return float(qty)
    for u in (f, t):
        if u not in KNOWN_UNITS:
            raise UomConversionError(f"Unknown unit of measure '{u}'.")
    edges: dict[str, list[tuple[str, float]]] = {}

    def add(a, b, factor):
        if factor and factor > 0:
            edges.setdefault(a, []).append((b, factor))
            edges.setdefault(b, []).append((a, 1.0 / factor))

    for (a, b), v in (item_factors or {}).items():
        add(norm(a), norm(b), float(v))
    for table in DIMENSIONS.values():
        for a in table:
            for b in table:
                if a != b:
                    edges.setdefault(a, []).append((b, table[a] / table[b]))
    seen, frontier = {f: 1.0}, [f]
    while frontier:
        nxt = []
        for u in frontier:
            for v, k in edges.get(u, []):
                if v not in seen:
                    seen[v] = seen[u] * k
                    nxt.append(v)
        frontier = nxt
    if t in seen:
        return float(qty) * seen[t]
    df, dt_ = dimension_of(f), dimension_of(t)
    if df and dt_ and df != dt_:
        raise UomConversionError(f"Cannot convert {f} ({df}) to {t} ({dt_}): different dimensions.")
    raise UomConversionError(f"No conversion registered for {f} -> {t}. Refusing to guess.")


def to_base(qty: float, from_uom: str, base_uom: str, item_factors: dict | None = None) -> float:
    return convert(qty, from_uom, base_uom, item_factors)
