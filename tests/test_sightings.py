"""Private Pet Sightings (SPEC 5.4, 16.7): creation, validation, prefill from a scan,
privacy (404 for everyone who may not see a sighting), access logging, finder
controls, the JSON API and the expiry job."""

from __future__ import annotations

import io
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from conftest import TACLOBAN, jpeg_bytes
from markupsafe import escape

from app.extensions import db
from app.models import (
    AuditLog,
    Job,
    Photo,
    PhotoOwner,
    Scan,
    ScanStatus,
    Sighting,
    SightingAccessLog,
)
from app.routes.sightings import expire_sightings
from app.services import job_service
from app.utils import strings
from app.utils.time import local_now, utcnow


def form_data(**overrides: Any) -> dict[str, Any]:
    """A valid sighting form (Manila local date/time an hour ago)."""
    seen = local_now() - timedelta(hours=1)
    data: dict[str, Any] = {
        "species": "dog",
        "breed": "Aspin",
        "colors": "Brown and white",
        "size": "medium",
        "condition": "healthy",
        "is_holding_animal": "1",
        "notes": "Friendly, red collar.",
        "lat": str(TACLOBAN[0]),
        "lng": str(TACLOBAN[1]),
        "place": "Brgy. 37, near the chapel",
        "seen_date": seen.strftime("%Y-%m-%d"),
        "seen_time": seen.strftime("%H:%M"),
        "contact_name": "Claricel Albotra",
        "contact_email": "claricel@example.com",
        "contact_phone": "0917 123 4567",
        "contact_pref": "phone",
        "consent": "1",
        "photos": (io.BytesIO(jpeg_bytes()), "dog.jpg"),
    }
    for key, value in overrides.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


def post_new(client, url="/sightings/new", **overrides):
    return client.post(url, data=form_data(**overrides), content_type="multipart/form-data")


def count(model) -> int:
    return db.session.scalar(sa.select(sa.func.count(model.id)))


def make_found_scan(user, make_photo, *, status=ScanStatus.COMPLETE, mode="found") -> Scan:
    """A completed Found Pet Scan with an uploaded scan photo (like scanner.perform_scan)."""
    photo = make_photo(PhotoOwner.SCAN, None, commit=False)
    scan = Scan(
        user_id=user.id,
        mode=mode,
        query_photo_id=photo.id,
        status=status,
        scan_lat=TACLOBAN[0] + 0.001,
        scan_lng=TACLOBAN[1] + 0.001,
        precheck={"species": "cat", "image_quality": "good"},
        attributes={
            "species": "cat",
            "breed_guess": "Puspin",
            "primary_colors": ["orange", "white"],
            "coat_pattern": "tabby",
            "size_estimate": "small",
        },
    )
    db.session.add(scan)
    db.session.flush()
    photo.attach_to(PhotoOwner.SCAN, scan.id)
    db.session.commit()
    return scan


@pytest.fixture(autouse=True)
def _gemini(fake_gemini):
    """Background matching runs inline (JOBS_EAGER); never let it reach a real API."""
    return fake_gemini


# --- Create (page) ----------------------------------------------------------------------------


def test_new_page_requires_login(client):
    response = client.get("/sightings/new")
    assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_new_page_prefills_account_contact(client, make_user, login):
    user = make_user(full_name="Jonalie Montera", email="jonalie@example.com")
    login(client, user)
    html = client.get("/sightings/new").get_data(as_text=True)
    assert "Report a" in html and "Pet Sighting" in html
    assert 'value="Jonalie Montera"' in html and 'value="jonalie@example.com"' in html
    assert f'value="{user.mobile_e164}"' in html
    assert "Where did you find this pet?" in html and "When did you find this pet?" in html
    assert "Your Information" in html
    assert str(escape(strings.SIGHTING_CONSENT_LABEL)) in html
    assert "data-location-picker" in html
    assert 'type="date"' in html and 'type="time"' in html


@pytest.mark.config(JOBS_EAGER=False)
def test_create_sighting(client, user, login, app):
    login(client, user)
    response = post_new(client)
    assert response.status_code == 302
    sighting = db.session.scalar(sa.select(Sighting))
    assert response.headers["Location"].endswith(f"/sightings/{sighting.id}?created=1")
    assert sighting.finder_id == user.id and sighting.status == "active"
    assert sighting.species == "dog" and sighting.condition == "healthy"
    assert sighting.is_holding_animal is True
    assert sighting.contact_phone == "+639171234567" and sighting.contact_pref == "phone"
    assert sighting.seen_place == "Brgy. 37, near the chapel"
    days = app.config["SIGHTING_EXPIRY_DAYS"]
    assert abs(sighting.expires_at - (utcnow() + timedelta(days=days))) < timedelta(minutes=5)
    assert len(sighting.photos) == 1 and sighting.primary_photo.is_primary
    job = db.session.scalar(sa.select(Job).where(Job.type == "match_sighting"))
    assert job.payload == {"sighting_id": sighting.id}
    assert (
        db.session.scalar(sa.select(AuditLog).where(AuditLog.event == "sighting.created")).target_id
        == sighting.id
    )

    page = client.get(f"/sightings/{sighting.id}?created=1").get_data(as_text=True)
    assert "Your sighting was saved" in page and "private" in page


def test_create_runs_background_matching_inline(client, user, login, monkeypatch):
    from app.services import matching_service

    calls = []
    monkeypatch.setattr(matching_service, "match_new_sighting", lambda sid: calls.append(sid) or [])
    login(client, user)
    assert post_new(client).status_code == 302
    assert calls == [db.session.scalar(sa.select(Sighting.id))]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"lat": None, "lng": None}, "Please pin a location"),
        ({"lat": "14.5995", "lng": "120.9842"}, "outside Tacloban"),
        ({"seen_date": (local_now() + timedelta(days=2)).strftime("%Y-%m-%d")}, "future"),
        ({"seen_date": None}, "Please enter the date"),
        ({"consent": None}, "Please agree"),
        ({"contact_phone": "", "contact_pref": "phone"}, "Add a contact number"),
        ({"contact_phone": "12345"}, "mobile"),
        ({"contact_email": "not-an-email"}, "valid email"),
        ({"species": "bird"}, "Invalid choice"),
        ({"contact_name": ""}, "This field is required"),
        ({"photos": None}, "at least one photo"),
        ({"photos": (io.BytesIO(b"no image"), "x.jpg")}, "not a readable photo"),
    ],
)
def test_create_validation(client, user, login, app, overrides, message):
    login(client, user)
    response = post_new(client, **overrides)
    assert response.status_code == 422
    assert message.lower() in response.get_data(as_text=True).lower()
    assert count(Sighting) == 0 and count(Photo) == 0


def test_invalid_second_photo_removes_the_first_file(client, user, login, app):
    from pathlib import Path

    login(client, user)
    response = client.post(
        "/sightings/new",
        data=form_data(
            photos=[(io.BytesIO(jpeg_bytes()), "a.jpg"), (io.BytesIO(b"nope"), "b.jpg")]
        ),
        content_type="multipart/form-data",
    )
    assert response.status_code == 422
    assert count(Sighting) == 0 and count(Photo) == 0
    assert [p for p in Path(app.config["UPLOAD_FOLDER"]).rglob("*.jpg")] == []


@pytest.mark.config(RATELIMIT_ENABLED=True, RATE_LIMIT_SIGHTINGS="1 per hour")
def test_sighting_creation_is_rate_limited(client, user, login):
    login(client, user)
    assert post_new(client).status_code == 302
    assert post_new(client).status_code == 429
    assert client.get("/sightings/new").status_code == 200  # GET is not limited


def test_sighting_form_requires_csrf(csrf_client, user, login):
    login(csrf_client, user)
    assert post_new(csrf_client).status_code == 400
    api = csrf_client.post("/api/sightings", data=form_data(), content_type="multipart/form-data")
    assert api.status_code == 400
    assert count(Sighting) == 0


# --- Prefill from a Found Pet Scan -------------------------------------------------------------


def test_prefill_from_own_scan(client, user, make_photo, login):
    scan = make_found_scan(user, make_photo)
    login(client, user)
    html = client.get(f"/sightings/new?scan_id={scan.id}").get_data(as_text=True)
    assert "The photo you scanned will be attached" in html
    assert 'value="cat" checked' in html
    assert 'value="Puspin"' in html and "orange, white (tabby)" in html
    assert f'name="scan_id" value="{scan.id}"' in html


def test_create_from_scan_reowns_the_scan_photo(client, user, make_photo, login):
    scan = make_found_scan(user, make_photo)
    photo_id = scan.query_photo_id
    login(client, user)

    response = post_new(client, url=f"/sightings/new?scan_id={scan.id}", photos=None, species="cat")

    assert response.status_code == 302
    sighting = db.session.scalar(sa.select(Sighting))
    photo = db.session.get(Photo, photo_id)
    assert (photo.owner_type, photo.owner_id) == (PhotoOwner.SIGHTING, sighting.id)
    assert photo.is_primary and sighting.primary_photo.id == photo_id
    assert sighting.ai_attributes["breed_guess"] == "Puspin"
    assert count(Photo) == 1  # re-owned, not copied
    # The scan still shows its photo (now visible through the sighting's rules).
    assert client.get(f"/media/{photo.public_id}").status_code == 200

    # A second attempt goes to the existing sighting instead of creating another one.
    again = client.get(f"/sightings/new?scan_id={scan.id}")
    assert again.status_code == 302 and f"/sightings/{sighting.id}" in again.headers["Location"]
    results_html = client.get(f"/scans/{scan.id}").get_data(as_text=True)
    assert "You reported this as a sighting" in results_html


def test_scan_prefill_plus_extra_photos(client, user, make_photo, login):
    scan = make_found_scan(user, make_photo)
    login(client, user)
    post_new(client, url=f"/sightings/new?scan_id={scan.id}")
    sighting = db.session.scalar(sa.select(Sighting))
    photos = sighting.photos
    assert len(photos) == 2 and photos[0].id == scan.query_photo_id


@pytest.mark.parametrize("kind", ["other_user", "rejected", "lost_mode"])
def test_scan_prefill_only_for_own_completed_found_scans(
    client, user, make_user, make_photo, login, kind
):
    owner = make_user() if kind == "other_user" else user
    scan = make_found_scan(
        owner,
        make_photo,
        status=ScanStatus.REJECTED if kind == "rejected" else ScanStatus.COMPLETE,
        mode="lost" if kind == "lost_mode" else "found",
    )
    login(client, user)
    html = client.get(f"/sightings/new?scan_id={scan.id}").get_data(as_text=True)
    assert "The photo you scanned will be attached" not in html
    # Posting with that scan id and no photo must not steal the scan photo.
    response = post_new(client, url=f"/sightings/new?scan_id={scan.id}", photos=None)
    assert response.status_code == 422
    assert db.session.get(Photo, scan.query_photo_id).owner_type == PhotoOwner.SCAN


# --- Privacy: who may see a sighting ----------------------------------------------------------


def test_finder_and_admin_can_view(client, make_sighting, make_admin, login):
    sighting = make_sighting()
    login(client, sighting.finder)
    html = client.get(f"/sightings/{sighting.id}").get_data(as_text=True)
    assert "Manage sighting" in html and "Mark as matched" in html
    assert count(SightingAccessLog) == 0  # the finder's own views are not logged

    admin = make_admin()
    login(client, admin)
    assert client.get(f"/sightings/{sighting.id}").status_code == 200
    log = db.session.scalar(sa.select(SightingAccessLog))
    assert log.viewer_id == admin.id and log.scan_id is None


def test_guest_is_sent_to_login(client, make_sighting):
    sighting = make_sighting()
    response = client.get(f"/sightings/{sighting.id}")
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    assert client.get(f"/api/sightings/{sighting.id}").status_code == 401


def test_other_users_get_404_not_403(client, make_sighting, make_user, make_lost_report, login):
    sighting = make_sighting()
    other_finder = make_user()
    owner_without_result = make_user()
    make_lost_report(owner=owner_without_result)  # owns a case, but no scan returned it
    for viewer in (other_finder, owner_without_result):
        login(client, viewer)
        assert client.get(f"/sightings/{sighting.id}").status_code == 404
        api = client.get(f"/api/sightings/{sighting.id}")
        assert api.status_code == 404 and api.get_json()["error"]["code"] == "not_found"
        assert client.get(f"/sightings/{sighting.id}/edit").status_code == 404
        assert (
            client.post(f"/sightings/{sighting.id}/status", data={"status": "matched"}).status_code
            == 404
        )
    # A missing sighting looks exactly the same.
    assert client.get("/sightings/999999").status_code == 404
    assert count(SightingAccessLog) == 0


def test_sighting_photo_is_private(client, make_sighting, make_user, login):
    sighting = make_sighting()
    key = sighting.primary_photo.public_id
    assert client.get(f"/media/{key}").status_code == 404  # guest
    login(client, make_user())
    assert client.get(f"/media/{key}").status_code == 404


def test_owner_with_scan_result_can_view_and_is_logged(
    client, make_user, make_sighting, make_lost_report, make_scan_result, login
):
    owner = make_user()
    report = make_lost_report(owner=owner)
    sighting = make_sighting(contact_pref="in_app", contact_email="finder@example.com")
    result = make_scan_result(owner, sighting, final_score=86)
    scan = db.session.get(Scan, result.scan_id)
    scan.source_lost_report_id = report.id
    db.session.commit()
    login(client, owner)

    response = client.get(f"/sightings/{sighting.id}")

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Why can you see this sighting?" in html and strings.DISCLAIMER in html
    assert "Message finder" in html
    assert f"sighting_id={sighting.id}" in html and f"lost_report_id={report.id}" in html
    assert sighting.contact_phone not in html  # in-app only
    assert "finder@example.com" not in html  # email is never shown to owners
    assert "Manage sighting" not in html
    log = db.session.scalar(sa.select(SightingAccessLog))
    assert (log.viewer_id, log.sighting_id, log.scan_id) == (owner.id, sighting.id, scan.id)
    audit = db.session.scalar(sa.select(AuditLog).where(AuditLog.event == "sighting.access"))
    assert audit.actor_id == owner.id and audit.target_id == sighting.id

    # Owners can see but not manage.
    assert client.get(f"/sightings/{sighting.id}/edit").status_code == 403
    assert (
        client.post(f"/sightings/{sighting.id}/status", data={"status": "matched"}).status_code
        == 403
    )


def test_background_match_also_grants_access(
    client, make_user, make_sighting, make_scan_result, login
):
    owner = make_user()
    sighting = make_sighting()
    make_scan_result(owner, sighting, trigger="background")
    login(client, owner)
    assert client.get(f"/sightings/{sighting.id}").status_code == 200


def test_owner_sees_phone_only_when_finder_chose_phone(
    client, make_user, make_sighting, make_scan_result, login
):
    owner = make_user()
    sighting = make_sighting(contact_pref="phone", contact_phone="+639181112222")
    make_scan_result(owner, sighting)
    login(client, owner)
    html = client.get(f"/sightings/{sighting.id}").get_data(as_text=True)
    assert "tel:+639181112222" in html
    data = client.get(f"/api/sightings/{sighting.id}").get_json()["sighting"]
    assert data["contact"]["phone"] == "+639181112222"
    assert "email" not in data["contact"]


def test_hidden_sighting_only_for_finder_and_admin(
    client, make_user, make_admin, make_sighting, make_scan_result, login
):
    owner = make_user()
    sighting = make_sighting(is_hidden=True, hidden_reason="Spam")
    make_scan_result(owner, sighting)
    login(client, owner)
    assert client.get(f"/sightings/{sighting.id}").status_code == 404
    login(client, sighting.finder)
    html = client.get(f"/sightings/{sighting.id}").get_data(as_text=True)
    assert "Hidden by a moderator" in html and "Spam" in html
    login(client, make_admin())
    assert client.get(f"/sightings/{sighting.id}").status_code == 200


# --- JSON API ------------------------------------------------------------------------------------


def test_api_create(client, user, login):
    login(client, user)
    response = client.post("/api/sightings", data=form_data(), content_type="multipart/form-data")
    assert response.status_code == 201
    body = response.get_json()
    sighting = db.session.get(Sighting, body["id"])
    assert sighting.finder_id == user.id
    assert body["url"].startswith(f"/sightings/{sighting.id}")
    assert body["status"] == "active" and body["expires_at"].endswith("Z")


def test_api_create_validation(client, user, login):
    login(client, user)
    response = client.post(
        "/api/sightings",
        data=form_data(lat=None, lng=None, consent=None),
        content_type="multipart/form-data",
    )
    assert response.status_code == 422
    error = response.get_json()["error"]
    assert error["code"] == "validation_error"
    assert set(error["details"]["fields"]) >= {"location", "consent"}


def test_api_create_from_scan(client, user, make_photo, login):
    scan = make_found_scan(user, make_photo)
    login(client, user)
    data = form_data(photos=None, scan_id=str(scan.id))
    first = client.post("/api/sightings", data=data, content_type="multipart/form-data")
    assert first.status_code == 201
    again = client.post(
        "/api/sightings",
        data=form_data(photos=None, scan_id=str(scan.id)),
        content_type="multipart/form-data",
    )
    assert again.status_code == 409
    assert again.get_json()["error"]["details"]["sighting_id"] == first.get_json()["id"]


def test_api_mine_lists_only_own_sightings(client, user, make_sighting, login):
    mine = make_sighting(finder=user, seen_place="Mine")
    make_sighting(seen_place="Someone else's")
    login(client, user)
    body = client.get("/api/sightings/mine").get_json()
    assert [s["id"] for s in body["sightings"]] == [mine.id]
    assert body["sightings"][0]["thumb_url"].startswith("/media/sightings/")


def test_api_detail_for_finder(client, make_sighting, login):
    sighting = make_sighting(contact_email="f@example.com")
    login(client, sighting.finder)
    data = client.get(f"/api/sightings/{sighting.id}").get_json()["sighting"]
    assert data["id"] == sighting.id and data["is_mine"] is True
    assert data["contact"]["email"] == "f@example.com"
    assert data["view_url"].startswith("https://www.google.com/maps/search/?api=1&query=")
    assert count(SightingAccessLog) == 0


def test_api_detail_logs_owner_access(client, make_user, make_sighting, make_scan_result, login):
    owner = make_user()
    sighting = make_sighting()
    result = make_scan_result(owner, sighting)
    login(client, owner)
    assert client.get(f"/api/sightings/{sighting.id}").status_code == 200
    log = db.session.scalar(sa.select(SightingAccessLog))
    assert log.viewer_id == owner.id and log.scan_id == result.scan_id


def test_sightings_are_not_listed_publicly(client, make_sighting):
    sighting = make_sighting(seen_place="Very private place")
    for path in ("/", "/lost-pets", "/api/lost-reports"):
        response = client.get(path)
        assert "Very private place" not in response.get_data(as_text=True), path
    assert client.get("/api/sightings/mine").status_code == 401
    assert sighting.id


# --- Finder controls -----------------------------------------------------------------------------


def test_finder_marks_matched_withdraws_and_reactivates(client, make_sighting, login, app):
    sighting = make_sighting()
    login(client, sighting.finder)

    client.post(f"/sightings/{sighting.id}/status", data={"status": "matched"})
    assert db.session.get(Sighting, sighting.id).status == "matched"

    client.post(f"/sightings/{sighting.id}/status", data={"status": "active"})
    assert db.session.get(Sighting, sighting.id).status == "active"

    client.post(f"/sightings/{sighting.id}/status", data={"status": "expired"})
    refreshed = db.session.get(Sighting, sighting.id)
    assert refreshed.status == "expired"
    assert db.session.scalar(sa.select(AuditLog).where(AuditLog.event == "sighting.withdrawn"))

    refreshed.expires_at = utcnow() - timedelta(days=1)
    db.session.commit()
    client.post(f"/sightings/{sighting.id}/status", data={"status": "active"})
    refreshed = db.session.get(Sighting, sighting.id)
    assert refreshed.status == "active" and refreshed.expires_at > utcnow() + timedelta(days=29)


def test_invalid_status_change_is_refused(client, make_sighting, login):
    sighting = make_sighting(status="expired")
    login(client, sighting.finder)
    response = client.post(
        f"/sightings/{sighting.id}/status", data={"status": "matched"}, follow_redirects=True
    )
    assert "not allowed" in response.get_data(as_text=True)
    assert db.session.get(Sighting, sighting.id).status == "expired"


def test_finder_edits_sighting(client, make_sighting, login):
    sighting = make_sighting()
    login(client, sighting.finder)
    page = client.get(f"/sightings/{sighting.id}/edit")
    assert page.status_code == 200 and "Edit" in page.get_data(as_text=True)
    data = form_data(
        photos=None, consent=None, notes="Now wearing a blue collar", condition="injured"
    )
    response = client.post(f"/sightings/{sighting.id}/edit", data=data)
    assert response.status_code == 302
    updated = db.session.get(Sighting, sighting.id)
    assert updated.notes == "Now wearing a blue collar" and updated.condition == "injured"
    bad = client.post(f"/sightings/{sighting.id}/edit", data=form_data(photos=None, lat=None))
    assert bad.status_code == 422


# --- Expiry job ----------------------------------------------------------------------------------


def test_expire_sightings(make_sighting):
    old = make_sighting()
    old.expires_at = utcnow() - timedelta(minutes=1)
    fresh = make_sighting()
    matched = make_sighting(status="matched")
    matched.expires_at = utcnow() - timedelta(days=3)
    db.session.commit()

    assert expire_sightings() == 1

    assert db.session.get(Sighting, old.id).status == "expired"
    assert db.session.get(Sighting, fresh.id).status == "active"
    assert db.session.get(Sighting, matched.id).status == "matched"
    assert expire_sightings() == 0  # idempotent


@pytest.mark.config(JOBS_EAGER=False)
def test_expire_sightings_job_runs_from_worker(make_sighting, run_jobs):
    old = make_sighting()
    old.expires_at = utcnow() - timedelta(hours=1)
    db.session.commit()
    assert "expire_sightings" in job_service.load_handlers()
    job_service.enqueue("expire_sightings", {})
    db.session.commit()
    assert run_jobs() == 1
    assert db.session.get(Sighting, old.id).status == "expired"
    assert db.session.scalar(sa.select(Job)).status == "done"


def test_expired_sightings_drop_out_of_owner_results(
    client, make_user, make_sighting, make_scan_result, login
):
    owner = make_user()
    sighting = make_sighting(seen_place="Old sighting place")
    result = make_scan_result(owner, sighting)
    login(client, owner)
    assert "Old sighting place" in client.get(f"/scans/{result.scan_id}").get_data(as_text=True)
    sighting.expires_at = utcnow() - timedelta(minutes=1)
    db.session.commit()
    expire_sightings()
    assert "Old sighting place" not in client.get(f"/scans/{result.scan_id}").get_data(as_text=True)
