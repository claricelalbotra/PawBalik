"""Rescue Directory, opening hours, /api/providers and Request Rescue Help (SPEC 5.8)."""

from __future__ import annotations

from datetime import datetime

import pytest
import sqlalchemy as sa
from conftest import TACLOBAN, get_csrf_token

from app.extensions import db
from app.models import AssistanceProvider, AuditLog, RescueRequest, SightingAccessLog
from app.routes.rescue import (
    ALWAYS_OPEN,
    build_summary,
    open_state,
    parse_hours,
    safe_http_url,
    tel_link,
)
from app.utils.strings import EMERGENCY_NOTE

# 2026-09-21 is a Monday, 2026-09-26 a Saturday, 2026-09-27 a Sunday (local Manila time).
MON_10AM = datetime(2026, 9, 21, 10, 0)
MON_7PM = datetime(2026, 9, 21, 19, 0)
SAT_11AM = datetime(2026, 9, 26, 11, 0)
SAT_1PM = datetime(2026, 9, 26, 13, 0)
SUN_10AM = datetime(2026, 9, 27, 10, 0)


@pytest.fixture
def make_provider(app):
    def _make(**fields):
        defaults = {
            "name": "Test Vet",
            "type": "vet",
            "address": "Real St.",
            "barangay": "Downtown",
            "lat": TACLOBAN[0],
            "lng": TACLOBAN[1],
            "phones": "0917 123 4567",
            "hours_text": "Mon-Sat 8:00 AM - 5:00 PM",
            "is_24_7": False,
            "services": "Consultation",
            "is_active": True,
            "is_sample": False,
        }
        defaults.update(fields)
        provider = AssistanceProvider(**defaults)
        db.session.add(provider)
        db.session.commit()
        return provider

    return _make


def _provider(**fields) -> AssistanceProvider:
    defaults = {"name": "X", "type": "vet", "is_24_7": False}
    defaults.update(fields)
    return AssistanceProvider(**defaults)


# --- Opening hours ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("8:00 AM - 5:00 PM", [(frozenset(range(7)), 480, 1020)]),
        ("Mon-Sat 8AM-5PM", [(frozenset(range(6)), 480, 1020)]),
        ("8AM-5PM Mon-Sat", [(frozenset(range(6)), 480, 1020)]),
        ("Daily 7:00 AM - 7:00 PM", [(frozenset(range(7)), 420, 1140)]),
        ("Tue, Thu 9AM-3PM", [(frozenset({1, 3}), 540, 900)]),
        ("Weekdays 9am to 5pm", [(frozenset(range(5)), 540, 1020)]),
        ("08:00-17:00", [(frozenset(range(7)), 480, 1020)]),
        ("8-5PM", [(frozenset(range(7)), 480, 1020)]),
        ("24/7", ALWAYS_OPEN),
        ("Open 24 hours", ALWAYS_OPEN),
        ("", None),
        (None, None),
        ("By appointment", None),
        ("Message us anytime", None),
        ("Mon-Sat 8AM-5PM except holidays", None),
        ("13PM-5PM", None),
    ],
)
def test_parse_hours_formats(text, expected):
    assert parse_hours(text) == expected


def test_parse_hours_multiple_segments():
    spec = parse_hours("Mon-Fri 8AM-5PM; Sat 8AM-12PM; Sun closed")
    assert spec == [(frozenset(range(5)), 480, 1020), (frozenset({5}), 480, 720)]
    assert parse_hours("Mon-Sat 8AM-5PM, Sun 9AM-12PM")[1] == (frozenset({6}), 540, 720)


def test_open_state_simple_hours():
    p = _provider(hours_text="Mon-Sat 8:00 AM - 5:00 PM")
    assert open_state(p, MON_10AM) is True
    assert open_state(p, MON_7PM) is False
    assert open_state(p, SUN_10AM) is False


def test_open_state_multi_segment_and_closed_day():
    p = _provider(hours_text="Mon-Fri 8AM-5PM; Sat 8AM-12PM; Sun closed")
    assert open_state(p, SAT_11AM) is True
    assert open_state(p, SAT_1PM) is False
    assert open_state(p, SUN_10AM) is False


def test_open_state_24_7_and_unknown():
    assert open_state(_provider(is_24_7=True, hours_text="whatever"), SUN_10AM) is True
    assert open_state(_provider(hours_text="24/7 hotline"), MON_7PM) is True
    assert open_state(_provider(hours_text="Call first"), MON_10AM) is None  # unknown, not open
    assert open_state(_provider(hours_text=None), MON_10AM) is None


def test_open_state_overnight_range():
    p = _provider(hours_text="Mon 6PM - 2AM")
    assert open_state(p, MON_7PM) is True
    assert open_state(p, datetime(2026, 9, 22, 1, 0)) is True  # Tuesday 1 AM, Monday's shift
    assert open_state(p, datetime(2026, 9, 22, 3, 0)) is False


# --- Safe links -----------------------------------------------------------------------------


def test_tel_link_is_sanitized():
    assert tel_link("0917 123 4567") == "tel:+639171234567"
    assert tel_link("(053) 321-1234") == "tel:0533211234"
    assert tel_link("+63 53 321 1234") == "tel:+63533211234"
    assert tel_link("javascript:alert(1)") is None
    assert tel_link("12") is None
    assert tel_link("") is None


def test_safe_http_url_only_http_https():
    assert safe_http_url("https://www.facebook.com/page") == "https://www.facebook.com/page"
    assert safe_http_url("http://fb.com/x") == "http://fb.com/x"
    assert safe_http_url("javascript:alert(1)") is None
    assert safe_http_url("data:text/html,hi") is None
    assert safe_http_url("//evil.example/x") is None
    assert safe_http_url("facebook.com/page") is None
    assert safe_http_url("https://") is None
    assert safe_http_url("https://x.com/a b") is None


# --- Directory page --------------------------------------------------------------------------


def test_directory_is_public_and_shows_emergency_note(client, make_provider):
    make_provider(name="SAMPLE Vet Downtown", is_sample=True)
    response = client.get("/rescue")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "PawBalik does not replace emergency services" in html
    assert EMERGENCY_NOTE.split(".")[0] in html
    assert "SAMPLE Vet Downtown" in html
    assert "chip--sample" in html
    assert 'href="tel:+639171234567"' in html
    assert "https://www.google.com/maps/search/?api=1&amp;query=" in html
    assert "https://www.google.com/maps/dir/?api=1&amp;destination=" in html
    assert "Use my current location" in html


def test_directory_hides_inactive_and_unsafe_facebook(client, make_provider):
    make_provider(name="Gone Clinic", is_active=False)
    make_provider(name="Bad Link Clinic", facebook_url="javascript:alert(1)")
    make_provider(name="Good Link Clinic", facebook_url="https://www.facebook.com/goodclinic")
    html = client.get("/rescue").get_data(as_text=True)
    assert "Gone Clinic" not in html
    assert "javascript:alert" not in html
    assert 'href="https://www.facebook.com/goodclinic"' in html


def test_directory_sorted_by_distance(client, make_provider):
    make_provider(name="Far Shelter", type="shelter", lat=11.30, lng=125.03)
    make_provider(name="Near Vet", lat=11.2440, lng=125.0050)
    make_provider(name="Middle Rescuer", type="rescuer", lat=11.26, lng=125.01)
    make_provider(name="No Pin Group", type="rescuer", lat=None, lng=None)
    html = client.get(f"/rescue?near={TACLOBAN[0]},{TACLOBAN[1]}").get_data(as_text=True)
    order = [html.index(n) for n in ("Near Vet", "Middle Rescuer", "Far Shelter", "No Pin Group")]
    assert order == sorted(order)
    assert "Sorted by distance from your location" in html
    assert " km</strong>" in html


def test_directory_type_and_open_filters(client, make_provider):
    make_provider(name="Vet One", type="vet", hours_text="Daily 12:00 AM - 11:59 PM")
    make_provider(name="Shelter One", type="shelter", hours_text="Daily 12:00 AM - 11:59 PM")
    make_provider(name="Unknown Hours Rescuer", type="rescuer", hours_text="Call first")
    make_provider(name="Always Open", type="vet", is_24_7=True, hours_text=None)

    html = client.get("/rescue?type=shelter").get_data(as_text=True)
    assert "Shelter One" in html and "Vet One" not in html

    html = client.get("/rescue?open=24_7").get_data(as_text=True)
    assert "Always Open" in html and "Vet One" not in html

    html = client.get("/rescue?open=now").get_data(as_text=True)
    assert "Unknown Hours Rescuer" not in html  # unknown hours never count as open
    assert "Always Open" in html


def test_directory_bad_origin_falls_back(client, make_provider):
    make_provider()
    response = client.get("/rescue?near=not-a-point")
    assert response.status_code == 200
    assert "Showing all providers instead" in response.get_data(as_text=True)


def test_directory_sorted_from_case_location(client, make_provider, make_lost_report):
    report = make_lost_report(last_seen_lat=11.30, last_seen_lng=125.03)
    make_provider(name="Close To Case", lat=11.2990, lng=125.0290)
    make_provider(name="City Hall Vet", lat=TACLOBAN[0], lng=TACLOBAN[1])
    html = client.get(f"/rescue?lost_report_id={report.id}").get_data(as_text=True)
    assert html.index("Close To Case") < html.index("City Hall Vet")
    assert report.case_number in html


def test_directory_sighting_origin_needs_permission(
    client, login, make_user, make_provider, make_sighting, make_scan_result
):
    make_provider(name="Clinic A")
    sighting = make_sighting(seen_place="Secret Spot")
    stranger, owner = make_user(), make_user()

    login(client, stranger)
    html = client.get(f"/rescue?sighting_id={sighting.id}").get_data(as_text=True)
    assert "That sighting is not available" in html
    assert "Secret Spot" not in html

    make_scan_result(owner, sighting)  # the owner's Lost Pet Scanner returned it
    login(client, owner)
    html = client.get(f"/rescue?sighting_id={sighting.id}").get_data(as_text=True)
    assert "Sorted by distance from pet sighting" in html
    assert (
        db.session.scalar(
            sa.select(sa.func.count(SightingAccessLog.id)).where(
                SightingAccessLog.viewer_id == owner.id
            )
        )
        == 1
    )


def test_guest_cannot_use_sighting_origin(client, make_provider, make_sighting):
    make_provider()
    sighting = make_sighting()
    html = client.get(f"/rescue?sighting_id={sighting.id}").get_data(as_text=True)
    assert "That sighting is not available" in html


def test_provider_detail_page(client, login, make_admin, make_provider):
    active = make_provider(name="Visible Clinic")
    hidden = make_provider(name="Hidden Clinic", is_active=False)
    assert client.get(f"/rescue/providers/{active.id}").status_code == 200
    assert client.get(f"/rescue/providers/{hidden.id}").status_code == 404
    assert client.get("/rescue/providers/9999").status_code == 404
    login(client, make_admin())
    response = client.get(f"/rescue/providers/{hidden.id}")
    assert response.status_code == 200
    assert "Inactive listing" in response.get_data(as_text=True)


# --- /api/providers ----------------------------------------------------------------------------


def test_api_providers_shape_and_sorting(client, make_provider):
    make_provider(name="Far", lat=11.30, lng=125.03, is_sample=True)
    near = make_provider(name="Near", lat=11.2440, lng=125.0050, facebook_url="ftp://x.example")
    response = client.get(f"/api/providers?near={TACLOBAN[0]},{TACLOBAN[1]}")
    assert response.status_code == 200
    data = response.get_json()
    assert data["count"] == 2
    assert data["origin"]["kind"] == "near"
    assert data["notice"] == EMERGENCY_NOTE
    first = data["providers"][0]
    assert first["id"] == near.id
    assert set(first) >= {
        "id", "name", "type", "type_label", "address", "barangay", "lat", "lng", "phones",
        "facebook_url", "hours_text", "is_24_7", "open_now", "services", "is_sample",
        "distance_km", "maps_url", "directions_url", "detail_url",
    }  # fmt: skip
    assert first["facebook_url"] is None  # ftp:// rejected
    assert first["phones"] == [{"display": "0917 123 4567", "tel": "tel:+639171234567"}]
    assert first["maps_url"].startswith("https://www.google.com/maps/search/?api=1&query=")
    assert first["distance_km"] < data["providers"][1]["distance_km"]
    assert data["providers"][1]["is_sample"] is True


def test_api_providers_filters_and_errors(client, make_provider):
    make_provider(name="Vet", type="vet", is_24_7=True)
    make_provider(name="Shelter", type="shelter", hours_text="by appointment")
    data = client.get("/api/providers?type=shelter").get_json()
    assert [p["name"] for p in data["providers"]] == ["Shelter"]
    assert data["origin"] is None and data["providers"][0]["distance_km"] is None

    data = client.get("/api/providers?open_now=1").get_json()
    assert [p["name"] for p in data["providers"]] == ["Vet"]

    bad_type = client.get("/api/providers?type=zoo")
    assert bad_type.status_code == 400
    assert bad_type.get_json()["error"]["code"] == "validation_error"
    assert client.get("/api/providers?near=abc").status_code == 400


# --- Request Rescue Help: authorization ------------------------------------------------------


def test_request_help_requires_login(client, make_lost_report):
    report = make_lost_report()
    response = client.get(f"/rescue/request?lost_report_id={report.id}")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_request_help_only_report_owner(client, login, make_user, make_lost_report):
    owner, other = make_user(), make_user()
    report = make_lost_report(owner=owner)
    login(client, other)
    assert client.get(f"/rescue/request?lost_report_id={report.id}").status_code == 403
    assert client.post(f"/rescue/request?lost_report_id={report.id}", data={}).status_code == 403
    login(client, owner)
    assert client.get(f"/rescue/request?lost_report_id={report.id}").status_code == 200


def test_request_help_admin_allowed(client, login, make_admin, make_lost_report):
    report = make_lost_report()
    login(client, make_admin())
    assert client.get(f"/rescue/request?lost_report_id={report.id}").status_code == 200


def test_request_help_bad_parameters(client, login, user, make_lost_report, make_sighting):
    login(client, user)
    assert client.get("/rescue/request").status_code == 302  # no subject: back to the directory
    report = make_lost_report(owner=user)
    sighting = make_sighting(finder=user)
    assert (
        client.get(
            f"/rescue/request?lost_report_id={report.id}&sighting_id={sighting.id}"
        ).status_code
        == 400
    )
    assert client.get("/rescue/request?lost_report_id=99999").status_code == 404


def test_request_help_hidden_report_not_found_for_others(
    client, login, make_user, make_lost_report
):
    report = make_lost_report(is_hidden=True)
    login(client, make_user())
    assert client.get(f"/rescue/request?lost_report_id={report.id}").status_code == 404


def test_request_help_sighting_policy(client, login, make_user, make_sighting, make_scan_result):
    finder, stranger, owner = make_user(), make_user(), make_user()
    sighting = make_sighting(finder=finder)
    url = f"/rescue/request?sighting_id={sighting.id}"

    login(client, stranger)
    assert client.get(url).status_code == 404  # existence not revealed

    login(client, finder)
    assert client.get(url).status_code == 200

    make_scan_result(owner, sighting)
    login(client, owner)
    assert client.get(url).status_code == 200


def test_request_help_hidden_sighting_blocks_owner(
    client, login, make_user, make_sighting, make_scan_result
):
    owner = make_user()
    sighting = make_sighting(is_hidden=True)
    make_scan_result(owner, sighting)
    login(client, owner)
    assert client.get(f"/rescue/request?sighting_id={sighting.id}").status_code == 404


# --- Request Rescue Help: summary ----------------------------------------------------------------


def test_summary_for_report_respects_privacy(app, make_user, make_lost_report):
    owner = make_user(full_name="Jonalie Montera", mobile_e164="+639171112222")
    report = make_lost_report(
        owner=owner, pet_name="Bantay", colors="Brown and white", show_phone=False
    )
    with app.test_request_context():
        text = build_summary(owner, report=report, note="Near the chapel")
    assert report.case_number in text
    assert "Dog · Aspin (mixed) · Brown and white · Medium · Male" in text
    assert "https://www.google.com/maps/search/?api=1&query=11.243700,125.004600" in text
    assert "Brgy. San Jose" in text
    assert "Contact: Jonalie Montera (phone not shared" in text
    assert "1112222" not in text
    assert "Note: Near the chapel" in text
    assert f"/cases/{report.id}" in text
    assert "possible matches only" in text

    with app.test_request_context():
        text = build_summary(owner, report=report, include_phone=True)
    assert "+63 917 111 2222" in text


def test_summary_for_sighting_viewer_uses_finder_preference(app, make_user, make_sighting):
    finder = make_user(full_name="Finder Fe", mobile_e164="+639179990000")
    viewer = make_user(full_name="Owner Ola")
    private = make_sighting(finder=finder, contact_pref="in_app", condition="injured")
    with app.test_request_context():
        text = build_summary(viewer, sighting=private)
    assert f"Pet Sighting #{private.id}" in text
    assert "Condition: Injured" in text
    assert "9990000" not in text
    assert "Finder: Finder Fe (phone not shared" in text
    assert "Requested by: Owner Ola" in text
    assert "/sightings/" not in text  # private: no link in a text meant for third parties

    shared = make_sighting(finder=finder, contact_pref="phone")
    with app.test_request_context():
        assert "+63 917 999 0000" in build_summary(viewer, sighting=shared)


def test_get_shows_preview_and_nearest_providers(
    client, login, make_user, make_lost_report, make_provider
):
    owner = make_user()
    report = make_lost_report(owner=owner)
    make_provider(name="Nearest Vet", lat=TACLOBAN[0], lng=TACLOBAN[1])
    login(client, owner)
    html = client.get(f"/rescue/request?lost_report_id={report.id}").get_data(as_text=True)
    assert report.case_number in html
    assert "Summary preview" in html
    assert "Nearest Vet" in html
    assert "Copy summary" in html
    assert EMERGENCY_NOTE.split(".")[0] in html


def test_post_logs_rescue_request_and_redirects(
    client, login, make_user, make_lost_report, make_provider
):
    owner = make_user()
    report = make_lost_report(owner=owner)
    provider = make_provider(name="Chosen Vet")
    login(client, owner)
    response = client.post(
        f"/rescue/request?lost_report_id={report.id}",
        data={"provider_id": provider.id, "note": "Please call after 5pm", "include_phone": "1"},
    )
    assert response.status_code == 302
    item = db.session.scalar(sa.select(RescueRequest))
    assert item.user_id == owner.id and item.lost_report_id == report.id
    assert item.provider_id == provider.id
    assert "To: Chosen Vet" in item.summary_text
    assert "Note: Please call after 5pm" in item.summary_text
    assert f"request_id={item.id}" in response.headers["Location"]
    assert db.session.scalar(sa.select(AuditLog).where(AuditLog.event == "rescue.request_created"))

    html = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Your rescue summary" in html
    assert "Please call after 5pm" in html


def test_post_rejects_unknown_provider_and_long_note(
    client, login, make_user, make_lost_report, make_provider
):
    owner = make_user()
    report = make_lost_report(owner=owner)
    inactive = make_provider(is_active=False)
    login(client, owner)
    url = f"/rescue/request?lost_report_id={report.id}"
    assert client.post(url, data={"provider_id": inactive.id}).status_code == 400
    assert client.post(url, data={"note": "x" * 600}).status_code == 400
    assert db.session.scalar(sa.select(sa.func.count(RescueRequest.id))) == 0


def test_saved_summary_of_another_user_is_not_shown(client, login, make_user, make_lost_report):
    owner = make_user()
    report = make_lost_report(owner=owner)
    other_item = RescueRequest(
        user_id=make_user().id, lost_report_id=report.id, summary_text="SECRET"
    )
    db.session.add(other_item)
    db.session.commit()
    login(client, owner)
    html = client.get(
        f"/rescue/request?lost_report_id={report.id}&request_id={other_item.id}"
    ).get_data(as_text=True)
    assert "SECRET" not in html


# --- /api/rescue-requests -----------------------------------------------------------------------


def test_api_create_request(client, login, make_user, make_lost_report, make_provider):
    owner = make_user()
    report = make_lost_report(owner=owner)
    provider = make_provider(name="API Vet")
    login(client, owner)
    response = client.post(
        "/api/rescue-requests",
        json={"lost_report_id": report.id, "provider_id": provider.id, "note": "hi"},
    )
    assert response.status_code == 201
    data = response.get_json()
    assert set(data) >= {"id", "summary", "provider"}
    assert data["provider"]["name"] == "API Vet"
    assert report.case_number in data["summary"]
    assert db.session.get(RescueRequest, data["id"]).summary_text == data["summary"]


def test_api_create_request_errors(client, login, make_user, make_lost_report, make_sighting):
    report = make_lost_report()
    sighting = make_sighting()
    assert (
        client.post("/api/rescue-requests", json={"lost_report_id": report.id}).status_code == 401
    )
    login(client, make_user())
    forbidden = client.post("/api/rescue-requests", json={"lost_report_id": report.id})
    assert forbidden.status_code == 403
    assert forbidden.get_json()["error"]["code"] == "forbidden"
    hidden = client.post("/api/rescue-requests", json={"sighting_id": sighting.id})
    assert hidden.status_code == 404
    both = client.post(
        "/api/rescue-requests", json={"lost_report_id": report.id, "sighting_id": sighting.id}
    )
    assert both.status_code == 400
    assert client.post("/api/rescue-requests", data="nope").status_code == 400
    assert db.session.scalar(sa.select(sa.func.count(RescueRequest.id))) == 0


def test_api_create_request_for_sighting_finder(client, login, make_user, make_sighting):
    finder = make_user()
    sighting = make_sighting(finder=finder)
    login(client, finder)
    response = client.post(
        "/api/rescue-requests", json={"sighting_id": sighting.id, "include_phone": True}
    )
    assert response.status_code == 201
    assert "Pet Sighting #" in response.get_json()["summary"]


def test_api_create_request_needs_csrf(csrf_client, login, make_user, make_lost_report):
    owner = make_user()
    report = make_lost_report(owner=owner)
    login(csrf_client, owner)
    denied = csrf_client.post("/api/rescue-requests", json={"lost_report_id": report.id})
    assert denied.status_code == 400
    token = get_csrf_token(csrf_client, "/rescue")
    allowed = csrf_client.post(
        "/api/rescue-requests",
        json={"lost_report_id": report.id},
        headers={"X-CSRFToken": token},
    )
    assert allowed.status_code == 201


@pytest.mark.config(RATELIMIT_ENABLED=True, RATE_LIMIT_RESCUE_REQUESTS="2 per hour")
def test_api_create_request_rate_limited(client, login, make_user, make_lost_report):
    owner = make_user()
    report = make_lost_report(owner=owner)
    login(client, owner)
    codes = [
        client.post("/api/rescue-requests", json={"lost_report_id": report.id}).status_code
        for _ in range(3)
    ]
    assert codes == [201, 201, 429]
