"""Google Maps URL builders (Maps URLs API - no key needed).

Mirrored in ``static/js/map.js``. These are the ONLY places Maps URLs are built.
Coordinates are formatted with 6 decimals (~0.1 m) and the comma is left
unescaped, exactly as in the Google Maps URLs documentation.
"""

from __future__ import annotations

TRAVEL_MODES = ("driving", "walking", "bicycling", "transit")


def _coords(lat: float, lng: float) -> str:
    return f"{float(lat):.6f},{float(lng):.6f}"


def view_url(lat: float, lng: float) -> str:
    """``https://www.google.com/maps/search/?api=1&query=LAT,LNG``"""
    return f"https://www.google.com/maps/search/?api=1&query={_coords(lat, lng)}"


def directions_url(lat: float, lng: float, mode: str = "driving") -> str:
    """``https://www.google.com/maps/dir/?api=1&destination=LAT,LNG&travelmode=MODE``

    The origin is omitted so Google Maps uses the device's current location.
    """
    if mode not in TRAVEL_MODES:
        raise ValueError(f"mode must be one of {TRAVEL_MODES}")
    return (
        "https://www.google.com/maps/dir/?api=1"
        f"&destination={_coords(lat, lng)}&travelmode={mode}"
    )
