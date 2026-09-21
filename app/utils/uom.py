"""Multi-UOM conversion helpers. Never silently mix units."""


class UomConversionError(Exception):
    pass


def convert(quantity, from_uom, to_uom, conversions=None):
    """Convert `quantity` from_uom -> to_uom.

    `conversions` is an optional dict[(from,to)] = factor built from
    UomConversion rows for a given item. If from == to, no lookup is needed.
    """
    from_uom = (from_uom or "EA").upper()
    to_uom = (to_uom or "EA").upper()
    if from_uom == to_uom:
        return quantity

    conversions = conversions or {}
    key = (from_uom, to_uom)
    if key in conversions:
        return quantity * conversions[key]

    reverse_key = (to_uom, from_uom)
    if reverse_key in conversions:
        return quantity / conversions[reverse_key]

    raise UomConversionError(
        f"No conversion registered for {from_uom} -> {to_uom}. "
        "Refusing to silently mix units."
    )


def build_conversion_map(uom_rows):
    """uom_rows: iterable of UomConversion model rows -> {(from,to): factor}"""
    m = {}
    for row in uom_rows:
        m[(row.from_uom.upper(), row.to_uom.upper())] = row.factor
    return m
