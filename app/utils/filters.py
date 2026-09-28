"""Jinja filters and globals.

Filters: ``timeago`` ("3 days ago"), ``date`` ("Sept 16, 2026"),
``datetime`` ("Sept 16, 2026, 3:45 PM"), ``time`` ("3:45 PM"), ``km``
("0.6 km"), ``masked_phone`` ("+63 9** *** 1234"), ``phone``
("+63 917 123 4567"), ``peso`` ("₱1,500"), ``label`` (enum value -> label).
Globals: ``view_url``, ``directions_url``, ``tel_href``, ``icon`` (inline SVG, app/utils/icons.py).

All datetime inputs are naive UTC and are shown in APP_TIMEZONE.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from flask import Flask

from app.models import enums
from app.utils.icons import icon
from app.utils.maps import directions_url, view_url
from app.utils.phone import format_phone, mask_phone, tel_href
from app.utils.time import to_local, utcnow

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "June", "July", "Aug", "Sept", "Oct", "Nov", "Dec")


def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}{'' if n == 1 else 's'} ago"


def timeago(value: datetime | None, now: datetime | None = None) -> str:
    """Naive-UTC datetime -> "just now", "5 minutes ago", "3 days ago", ..."""
    if value is None:
        return ""
    now = now or utcnow()
    seconds = int((now - value).total_seconds())
    if seconds < 0:
        return "just now" if seconds > -120 else "in the future"
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return _plural(minutes, "minute")
    hours = minutes // 60
    if hours < 24:
        return _plural(hours, "hour")
    days = hours // 24
    if days < 14:
        return _plural(days, "day")
    if days < 60:
        return _plural(days // 7, "week")
    if days < 365:
        return _plural(days // 30, "month")
    return _plural(days // 365, "year")


def format_date(value: datetime | date | None) -> str:
    """-> "Sept 16, 2026" (datetimes are converted to local time first)."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        value = to_local(value)
    return f"{MONTHS[value.month - 1]} {value.day}, {value.year}"


def format_time(value: datetime | None) -> str:
    """-> "3:45 PM" in local time."""
    if value is None:
        return ""
    local = to_local(value)
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def format_datetime(value: datetime | None) -> str:
    """-> "Sept 16, 2026, 3:45 PM" in local time."""
    if value is None:
        return ""
    return f"{format_date(value)}, {format_time(value)}"


def iso_local(value: datetime | None) -> str:
    """-> "2026-09-16T15:45" local, for <input type=datetime-local> and <time datetime>."""
    if value is None:
        return ""
    return to_local(value).strftime("%Y-%m-%dT%H:%M")


def km(value: float | None, decimals: int = 1) -> str:
    """-> "0.6 km" ("" if unknown)."""
    if value is None:
        return ""
    return f"{float(value):.{decimals}f} km"


def peso(value: Decimal | float | int | None) -> str:
    """-> "₱1,500" or "₱1,500.50"."""
    if value is None:
        return ""
    amount = Decimal(str(value))
    if amount == amount.to_integral_value():
        return f"₱{int(amount):,}"
    return f"₱{amount:,.2f}"


def enum_label(value: str | None, kind: str) -> str:
    """``{{ report.status|label('ReportStatus') }}`` -> "Lost"."""
    choices = getattr(enums, kind, None)
    return choices.label(value) if choices else (value or "")


def register_filters(app: Flask) -> None:
    app.jinja_env.filters.update(
        timeago=timeago,
        date=format_date,
        datetime=format_datetime,
        time=format_time,
        iso_local=iso_local,
        km=km,
        masked_phone=mask_phone,
        phone=format_phone,
        peso=peso,
        label=enum_label,
    )
    app.jinja_env.globals.update(
        view_url=view_url,
        directions_url=directions_url,
        tel_href=tel_href,
        icon=icon,
    )
