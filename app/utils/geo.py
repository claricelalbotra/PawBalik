"""Geographic helpers: Haversine distance and the Tacloban service area."""

from __future__ import annotations

import math

from flask import current_app, has_app_context

EARTH_RADIUS_KM = 6371.0088

# Fallback when called outside an app context (keep in sync with config.py).
DEFAULT_BOUNDS = {"south": 11.15, "west": 124.88, "north": 11.40, "east": 125.08}


def haversine_km(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    """Great-circle distance in kilometres between two WGS84 points."""
    phi1, phi2 = math.radians(a_lat), math.radians(b_lat)
    dphi = math.radians(b_lat - a_lat)
    dlmb = math.radians(b_lng - a_lng)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def service_area_bounds() -> dict[str, float]:
    """The configured bounding box: dict with south/west/north/east."""
    if has_app_context():
        return current_app.config.get("SERVICE_AREA_BOUNDS", DEFAULT_BOUNDS)
    return DEFAULT_BOUNDS


def in_service_area(lat: float | None, lng: float | None) -> bool:
    """True if (lat, lng) lies inside the Tacloban bounding box (inclusive)."""
    if lat is None or lng is None:
        return False
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return False
    if math.isnan(lat) or math.isnan(lng):
        return False
    b = service_area_bounds()
    return b["south"] <= lat <= b["north"] and b["west"] <= lng <= b["east"]


def distance_or_none(
    a_lat: float | None, a_lng: float | None, b_lat: float | None, b_lng: float | None
) -> float | None:
    """Haversine distance, or None if any coordinate is missing."""
    if None in (a_lat, a_lng, b_lat, b_lng):
        return None
    return haversine_km(a_lat, a_lng, b_lat, b_lng)


def parse_near(value: str | None) -> tuple[float, float] | None:
    """Parse a ``near=lat,lng`` query value; returns None if absent or invalid."""
    if not value:
        return None
    parts = value.split(",")
    if len(parts) != 2:
        return None
    try:
        lat, lng = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng
