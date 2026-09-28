"""Input validation helpers shared by forms and JSON APIs.

Functions raise :class:`ValidationError` (a ``ValueError``) with a friendly
message; routes turn it into a form error or ``json_error("validation_error", ...)``.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta

from flask import current_app, has_app_context

from app.utils.geo import in_service_area
from app.utils.time import local_to_utc, utcnow


class ValidationError(ValueError):
    """Invalid user input. ``field`` names the offending field (optional)."""

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.message = message
        self.field = field


PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128


def password_problems(
    password: str, *, email: str | None = None, name: str | None = None
) -> list[str]:
    """Return a list of problems with ``password`` (empty list = strong enough).

    Rule: 8-128 characters, at least one letter and one number, and not
    containing the email's user part or the user's name.
    """
    problems: list[str] = []
    pw = password or ""
    if len(pw) < PASSWORD_MIN_LENGTH:
        problems.append(f"Use at least {PASSWORD_MIN_LENGTH} characters.")
    if len(pw) > PASSWORD_MAX_LENGTH:
        problems.append(f"Use at most {PASSWORD_MAX_LENGTH} characters.")
    if not re.search(r"[A-Za-z]", pw):
        problems.append("Include at least one letter.")
    if not re.search(r"\d", pw):
        problems.append("Include at least one number.")
    lowered = pw.lower()
    local_part = (email or "").split("@")[0].lower()
    if len(local_part) >= 4 and local_part in lowered:
        problems.append("Do not use your email address in your password.")
    first = (name or "").split(" ")[0].lower()
    if len(first) >= 4 and first in lowered:
        problems.append("Do not use your name in your password.")
    return problems


def clean_text(
    value: str | None, *, max_length: int, required: bool = False, field: str | None = None
) -> str | None:
    """Strip whitespace; enforce length; return None for empty optional values."""
    text = (value or "").strip()
    if not text:
        if required:
            raise ValidationError("This field is required.", field)
        return None
    if len(text) > max_length:
        raise ValidationError(f"Must be at most {max_length} characters.", field)
    return text


def parse_choice(
    value: str | None, allowed: tuple[str, ...], *, field: str | None = None, required: bool = True
) -> str | None:
    value = (value or "").strip().lower()
    if not value:
        if required:
            raise ValidationError("Please choose an option.", field)
        return None
    if value not in allowed:
        raise ValidationError("Invalid choice.", field)
    return value


def parse_coordinates(
    lat_raw: object, lng_raw: object, *, require_service_area: bool = True
) -> tuple[float, float]:
    """Parse and validate a lat/lng pair (strings or numbers).

    With ``require_service_area`` the point must be inside the Tacloban
    bounding box (SPEC 3) unless config ``OUT_OF_AREA_POLICY`` is ``"warn"``.
    """
    try:
        lat = float(lat_raw)  # type: ignore[arg-type]
        lng = float(lng_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValidationError(
            "Please pin a location on the map or enter latitude and longitude.", "location"
        ) from None
    if math.isnan(lat) or math.isnan(lng) or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValidationError("Latitude/longitude are out of range.", "location")
    if require_service_area and not in_service_area(lat, lng):
        policy = (
            current_app.config.get("OUT_OF_AREA_POLICY", "reject")
            if has_app_context()
            else "reject"
        )
        if policy == "reject":
            raise ValidationError(
                "This location is outside Tacloban City. PawBalik currently serves Tacloban City only.",
                "location",
            )
    return lat, lng


def parse_local_datetime(
    date_raw: str | None,
    time_raw: str | None = None,
    *,
    field: str | None = None,
    allow_future: bool = False,
) -> datetime:
    """HTML ``<input type=date>`` + ``<input type=time>`` (Manila time) -> naive UTC."""
    try:
        d = date.fromisoformat((date_raw or "").strip())
    except ValueError:
        raise ValidationError("Enter a valid date.", field) from None
    t = time(0, 0)
    if time_raw and time_raw.strip():
        try:
            t = time.fromisoformat(time_raw.strip())
        except ValueError:
            raise ValidationError("Enter a valid time.", field) from None
    value = local_to_utc(datetime.combine(d, t))
    if not allow_future and value > utcnow() + timedelta(minutes=10):  # clock-skew grace
        raise ValidationError("The date and time cannot be in the future.", field)
    return value


def parse_bool(value: object) -> bool:
    """Form checkbox / JSON boolean -> bool."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def parse_int(value: object, *, default: int | None = None) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
