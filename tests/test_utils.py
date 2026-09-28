"""Unit tests for app/utils: phone, geo, maps, filters, case numbers, validators, http, config."""

from __future__ import annotations

import io
from datetime import datetime, timedelta

import pytest

from app.utils import geo, maps, phone
from app.utils.case_numbers import format_case_number, next_case_number, parse_case_number
from app.utils.filters import format_date, km, peso, timeago
from app.utils.validators import (
    ValidationError,
    parse_coordinates,
    parse_local_datetime,
    password_problems,
)

# --- phone -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "09171234567",
        "9171234567",
        "639171234567",
        "+639171234567",
        "+63 917 123 4567",
        "(0917) 123-4567",
        "0917.123.4567",
    ],
)
def test_normalize_ph_accepts_common_formats(raw):
    assert phone.normalize_ph(raw) == "+639171234567"


@pytest.mark.parametrize(
    "raw", ["", "   ", "12345", "0321234567", "+1 415 555 0101", "0917123456a", "091712345678"]
)
def test_normalize_ph_rejects_invalid(raw):
    with pytest.raises(ValueError):
        phone.normalize_ph(raw)


def test_mask_and_format_phone():
    assert phone.mask_phone("+639171234567") == "+63 9** *** 4567"
    assert phone.format_phone("+639171234567") == "+63 917 123 4567"
    assert phone.mask_phone(None) == ""
    assert phone.tel_href("+639171234567") == "tel:+639171234567"


# --- geo & maps ------------------------------------------------------------------------------


def test_haversine_known_distance():
    # Tacloban City Hall to Daniel Z. Romualdez Airport is roughly 3 km.
    d = geo.haversine_km(11.2437, 125.0046, 11.2276, 125.0279)
    assert 2.5 < d < 3.5
    assert geo.haversine_km(11.24, 125.0, 11.24, 125.0) == pytest.approx(0.0)


def test_in_service_area(app):
    assert geo.in_service_area(11.2437, 125.0046)  # downtown Tacloban
    assert geo.in_service_area(11.35, 124.95)  # northern barangays
    assert not geo.in_service_area(10.3157, 123.8854)  # Cebu City
    assert not geo.in_service_area(14.5995, 120.9842)  # Manila
    assert not geo.in_service_area(None, 125.0)
    assert not geo.in_service_area(float("nan"), 125.0)


def test_parse_near():
    assert geo.parse_near("11.24,125.00") == (11.24, 125.0)
    assert geo.parse_near("abc") is None
    assert geo.parse_near("95,10") is None
    assert geo.parse_near(None) is None


def test_maps_urls():
    assert (
        maps.view_url(11.2437, 125.0046)
        == "https://www.google.com/maps/search/?api=1&query=11.243700,125.004600"
    )
    assert maps.directions_url(11.2437, 125.0046) == (
        "https://www.google.com/maps/dir/?api=1&destination=11.243700,125.004600&travelmode=driving"
    )
    assert maps.directions_url(11.2, 125.0, "walking").endswith("travelmode=walking")
    with pytest.raises(ValueError):
        maps.directions_url(11.2, 125.0, "flying")


# --- filters ---------------------------------------------------------------------------------


def test_timeago():
    now = datetime(2026, 9, 20, 12, 0, 0)
    assert timeago(now - timedelta(seconds=20), now) == "just now"
    assert timeago(now - timedelta(minutes=5), now) == "5 minutes ago"
    assert timeago(now - timedelta(hours=1), now) == "1 hour ago"
    assert timeago(now - timedelta(days=3), now) == "3 days ago"
    assert timeago(now - timedelta(days=21), now) == "3 weeks ago"
    assert timeago(None) == ""


def test_date_km_peso(app):
    # 2026-09-16 02:00 UTC is Sept 16 10:00 in Manila.
    assert format_date(datetime(2026, 9, 16, 2, 0)) == "Sept 16, 2026"
    # 2026-09-15 20:00 UTC is already Sept 16 in Manila (UTC+8).
    assert format_date(datetime(2026, 9, 15, 20, 0)) == "Sept 16, 2026"
    assert km(0.6123) == "0.6 km"
    assert km(None) == ""
    assert peso(1500) == "₱1,500"
    assert peso(1500.5) == "₱1,500.50"


# --- case numbers -------------------------------------------------------------------------------


def test_case_number_format_and_parse():
    assert format_case_number(2026, 123) == "PB-2026-000123"
    assert parse_case_number("pb-2026-000123") == (2026, 123)
    assert parse_case_number("PB-26-1") is None


def test_next_case_number_is_sequential_per_year(app, db_session):
    assert next_case_number(2026) == "PB-2026-000001"
    assert next_case_number(2026) == "PB-2026-000002"
    assert next_case_number(2027) == "PB-2027-000001"
    db_session.commit()
    assert next_case_number(2026) == "PB-2026-000003"


def test_next_case_number_rollback_releases_number(app, db_session):
    assert next_case_number(2030) == "PB-2030-000001"
    db_session.commit()
    assert next_case_number(2030) == "PB-2030-000002"
    db_session.rollback()
    assert next_case_number(2030) == "PB-2030-000002"


def test_case_number_upsert_compiles_for_mysql():
    """The race-safe INSERT ... ON DUPLICATE KEY UPDATE renders on the MySQL dialect."""
    from sqlalchemy.dialects import mysql
    from sqlalchemy.dialects.mysql import insert

    from app.models import CaseCounter

    table = CaseCounter.__table__
    stmt = (
        insert(table)
        .values(year=2026, last_value=0)
        .on_duplicate_key_update(last_value=table.c.last_value)
    )
    sql = str(stmt.compile(dialect=mysql.dialect()))
    assert "ON DUPLICATE KEY UPDATE" in sql


# --- validators ---------------------------------------------------------------------------------


def test_password_problems():
    assert password_problems("Tacloban2026") == []
    assert password_problems("short1")
    assert password_problems("onlyletters")
    assert password_problems("1234567890")
    assert password_problems("juandelacruz99", email="juandelacruz@example.com")


def test_parse_coordinates(app):
    assert parse_coordinates("11.2437", "125.0046") == (11.2437, 125.0046)
    with pytest.raises(ValidationError):
        parse_coordinates("14.5995", "120.9842")  # Manila: outside Tacloban
    with pytest.raises(ValidationError):
        parse_coordinates("", "125.0")
    assert parse_coordinates("14.5995", "120.9842", require_service_area=False)


@pytest.mark.config(OUT_OF_AREA_POLICY="warn")
def test_parse_coordinates_warn_policy(app):
    assert parse_coordinates("14.5995", "120.9842") == (14.5995, 120.9842)


def test_parse_local_datetime(app):
    value = parse_local_datetime("2026-09-16", "10:30")
    assert value == datetime(2026, 9, 16, 2, 30)  # Manila is UTC+8
    with pytest.raises(ValidationError):
        parse_local_datetime("2999-01-01", "10:00")
    with pytest.raises(ValidationError):
        parse_local_datetime("not-a-date")


# --- http ---------------------------------------------------------------------------------------


def test_safe_redirect(app):
    from app.utils.http import is_safe_redirect_url

    with app.test_request_context("/", base_url="http://localhost"):
        assert is_safe_redirect_url("/my-cases")
        assert is_safe_redirect_url("/cases/3?x=1")
        assert not is_safe_redirect_url("https://evil.example.com/")
        assert not is_safe_redirect_url("//evil.example.com")
        assert not is_safe_redirect_url("/\\evil.example.com")
        assert not is_safe_redirect_url("javascript:alert(1)")
        assert not is_safe_redirect_url(None)


def test_json_error_shape(app):
    from app.utils.http import json_error

    with app.test_request_context("/api/x"):
        response, status = json_error("not_found", "No such case.", 404)
        assert status == 404
        assert response.get_json() == {"error": {"code": "not_found", "message": "No such case."}}


# --- config -------------------------------------------------------------------------------------


def test_dotenv_parses_spaces_around_equals():
    """.env uses `KEY = value`; python-dotenv must read it as KEY -> value."""
    from dotenv import dotenv_values

    values = dotenv_values(stream=io.StringIO("GEMINI_API_KEY = abc123\nOTHER=x\n"))
    assert values["GEMINI_API_KEY"] == "abc123"


def test_testing_config_never_uses_real_services(app):
    assert app.config["GEMINI_API_KEY"] is None
    assert app.config["SMS_PROVIDER"] == "memory"
    assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite://"
    assert app.config["JOBS_EAGER"] is True


def test_production_requires_secret_key(monkeypatch):
    from app import create_app

    with pytest.raises(RuntimeError):
        create_app("production", SECRET_KEY="dev-insecure-change-me")
    app = create_app("production", SECRET_KEY="x" * 40, SQLALCHEMY_DATABASE_URI="sqlite://")
    assert app.config["SESSION_COOKIE_SECURE"] is True


def test_service_area_bounds_cover_tacloban(app):
    b = app.config["SERVICE_AREA_BOUNDS"]
    center = app.config["SERVICE_AREA_CENTER"]
    assert b["south"] < center["lat"] < b["north"]
    assert b["west"] < center["lng"] < b["east"]
    assert 11.0 < b["south"] < 11.25 and 11.3 < b["north"] < 11.5


def test_icon_helper_is_safe():
    from app.utils.icons import icon

    html = str(icon("paw", label='Paw "icon"'))
    assert html.startswith("<svg") and "&#34;icon&#34;" in html
    assert 'aria-hidden="true"' in str(icon("home"))


def test_env_example_lists_every_config_key():
    """Every env var read in app/config.py appears in .env.example (maybe commented out)."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    config_src = (root / "app" / "config.py").read_text(encoding="utf-8")
    keys = set(re.findall(r'env_(?:str|bool|int|float)\(\s*"([A-Z0-9_]+)"', config_src))
    example = (root / ".env.example").read_text(encoding="utf-8")
    listed = set(re.findall(r"^#?\s*([A-Z0-9_]+)\s*=", example, flags=re.MULTILINE))
    assert keys - listed == set()
