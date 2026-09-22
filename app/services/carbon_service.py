"""Freight cost, transit time and CO2e estimates.

All emissions are ESTIMATES = shipped tonnes × distance (km) × configured emission factor (kg CO2e per tonne-km).
Factors are configuration (`carbon.factors`), not measurements; supply your own certified factors for reporting.
"""
from __future__ import annotations

from . import settings_service as S

DISCLAIMER = ("Estimate based on configured emission factors (kg CO2e per tonne-km) × tonnes × distance. "
              "Not a certified carbon footprint.")


def emissions_kg(weight_kg: float, distance_km: float, mode: str, factors: dict | None = None) -> float:
    factors = factors or S.get("carbon.factors")
    f = factors.get((mode or "ROAD").upper(), factors.get("ROAD", 0.062))
    return max(weight_kg, 0.0) / 1000.0 * max(distance_km, 0.0) * f


def freight_cost(weight_kg: float, distance_km: float, mode: str, rates: dict | None = None,
                 min_charge: float | None = None) -> float:
    rates = rates or S.get("freight.rate_per_tkm")
    min_charge = S.get("freight.min_charge") if min_charge is None else min_charge
    r = rates.get((mode or "ROAD").upper(), rates.get("ROAD", 4.5))
    if weight_kg <= 0 or distance_km <= 0:
        return 0.0
    return max(min_charge, weight_kg / 1000.0 * distance_km * r)


def transit_days(distance_km: float, mode: str) -> float:
    speed = S.get("freight.speed_kmpd").get((mode or "ROAD").upper(), 450.0)
    handling = S.get("freight.handling_days").get((mode or "ROAD").upper(), 1.0)
    return handling + distance_km / speed if distance_km > 0 else handling


def compare_modes(weight_kg: float, distance_km: float, base_mode: str = "ROAD") -> list[dict]:
    """Standard vs expedited freight for the same consignment."""
    exp_mode = S.get("freight.expedite_mode").get(base_mode, "AIR")
    out = []
    for label, mode in (("Standard freight", base_mode), ("Expedited freight", exp_mode)):
        out.append({"option": label, "mode": mode, "cost": freight_cost(weight_kg, distance_km, mode),
                    "co2e_kg": emissions_kg(weight_kg, distance_km, mode), "transit_days": transit_days(distance_km, mode)})
    return out
