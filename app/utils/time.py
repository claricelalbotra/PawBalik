"""Time helpers.

Convention (used everywhere in PawBalik):

* The database stores **naive UTC** datetimes (no tzinfo). Always create
  timestamps with :func:`utcnow` - never ``datetime.now()`` or
  ``datetime.utcnow()`` directly.
* Users see and type times in the app time zone (``APP_TIMEZONE``,
  Asia/Manila, UTC+8). Convert with :func:`to_local` for display and
  :func:`local_to_utc` for form input.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from flask import current_app, has_app_context

DEFAULT_TIMEZONE = "Asia/Manila"


def utcnow() -> datetime:
    """Current time as a *naive* UTC datetime (the DB storage convention)."""
    return datetime.now(UTC).replace(tzinfo=None)


def app_timezone() -> ZoneInfo:
    name = DEFAULT_TIMEZONE
    if has_app_context():
        name = current_app.config.get("APP_TIMEZONE", DEFAULT_TIMEZONE)
    return ZoneInfo(name)


def to_local(dt: datetime | None) -> datetime | None:
    """Naive-UTC (or aware) datetime -> aware datetime in the app time zone."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(app_timezone())


def local_now() -> datetime:
    """Current time in the app time zone (aware)."""
    return datetime.now(app_timezone())


def local_to_utc(dt: datetime) -> datetime:
    """A local wall-clock datetime (naive = app time zone) -> naive UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=app_timezone())
    return dt.astimezone(UTC).replace(tzinfo=None)


def combine_local(d: date, t: time | None = None) -> datetime:
    """Form date + optional time (local) -> naive UTC for storage."""
    return local_to_utc(datetime.combine(d, t or time(0, 0)))


def days_ago(days: float) -> datetime:
    """Naive UTC datetime ``days`` before now (handy for queries)."""
    return utcnow() - timedelta(days=days)


def start_of_local_day_utc(now: datetime | None = None) -> datetime:
    """Naive UTC instant of the most recent local midnight (for "per day" limits)."""
    local = to_local(now or utcnow())
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC).replace(tzinfo=None)
