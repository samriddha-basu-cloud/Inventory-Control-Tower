import math

ROAD_CIRCUITY = 1.3   # road distance ≈ 1.3 × great-circle distance


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    if None in (lat1, lon1, lat2, lon2):
        return 0.0
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def route_km(lat1, lon1, lat2, lon2, mode: str = "ROAD") -> float:
    d = haversine_km(lat1, lon1, lat2, lon2)
    return d * (ROAD_CIRCUITY if mode in ("ROAD", "RAIL") else 1.1)
