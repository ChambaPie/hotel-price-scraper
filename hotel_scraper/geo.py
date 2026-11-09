"""Distance and tiling helpers. Pure functions, no network."""
from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

EARTH_KM = 6371.0088
KM_PER_DEG_LAT = 111.32


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = radians(lat1), radians(lat2)
    dp, dl = p2 - p1, radians(lon2 - lon1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * EARTH_KM * asin(sqrt(a))


def bbox_for_radius(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """(south, west, north, east) enclosing the circle. Longitude span uses the widest latitude."""
    dlat = radius_km / KM_PER_DEG_LAT
    widest = min(89.0, max(abs(lat - dlat), abs(lat + dlat)))
    dlon = radius_km / (KM_PER_DEG_LAT * cos(radians(widest)))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def _tile_touches_circle(s, w, n, e, lat, lon, radius_km) -> bool:
    nearest_lat = min(max(lat, s), n)
    nearest_lon = min(max(lon, w), e)
    return haversine_km(lat, lon, nearest_lat, nearest_lon) <= radius_km


def tiles(lat: float, lon: float, radius_km: float, step_deg: float) -> list[tuple[float, float, float, float]]:
    """Grid tiles (s, w, n, e) covering the circle; tiles entirely outside it are dropped."""
    s0, w0, n0, e0 = bbox_for_radius(lat, lon, radius_km)
    out = []
    s = s0
    while s < n0:
        n = min(s + step_deg, n0)
        w = w0
        while w < e0:
            e = min(w + step_deg, e0)
            if _tile_touches_circle(s, w, n, e, lat, lon, radius_km):
                out.append((round(s, 4), round(w, 4), round(n, 4), round(e, 4)))
            w = e
        s = n
    return out
