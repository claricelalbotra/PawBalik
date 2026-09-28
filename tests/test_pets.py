"""Lost Pet Reports: wizard create, validation, case numbers, jobs, browse, edit, JSON API."""

from __future__ import annotations

import io
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import TACLOBAN, get_csrf_token

from app.extensions import db
from app.models import AuditLog, CaseStatusHistory, Job, LostReport, Photo, PhotoOwner
from app.utils.time import local_now, utcnow

# Jobs stay queued (not run inline) so these tests only check what the reports code does.
pytestmark = pytest.mark.config(JOBS_EAGER=False)

CASE_RE = re.compile(r"^PB-\d{4}-\d{6}$")


def yesterday() -> str:
    return (local_now() - timedelta(days=1)).strftime("%Y-%m-%d")


def form_data(upload, photos: int = 1, **overrides) -> dict:
    data = {
        "species": "dog",
        "pet_name": "Bantay",
        "breed": "Aspin (mixed)",
        "colors": "Brown with white chest",
        "size": "medium",
        "sex": "male",
        "age_text": "2 years",
        "identifying_features": "Red collar, white patch on left ear",
        "lat": str(TACLOBAN[0]),
        "lng": str(TACLOBAN[1]),
        "place": "Brgy. San Jose, near the chapel",
        "last_seen_date": yesterday(),
        "last_seen_time": "17:30",
        "contact_name": "Maria Santos",
        "contact_email": "maria@example.com",
        "contact_phone": "0917 555 1234",
        "reward_amount": "1500",
        "reward_text": "Plus snacks",
    }
    if photos:
        data["photos"] = [
            upload(f"pet{i}.jpg", color=(40 * i % 255, 120, 80)) for i in range(photos)
        ]
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


def post_new(client, data):
    return client.post("/lost-pets/new", data=data, content_type="multipart/form-data")


def jobs(job_type: str) -> list[Job]:
    return list(db.session.scalars(sa.select(Job).where(Job.type == job_type).order_by(Job.id)))


def report_count() -> int:
    return db.session.scalar(sa.select(sa.func.count(LostReport.id)))


# --- Wizard page ----------------------------------------------------------------------------


def test_new_requires_login(client):
    response = client.get("/lost-pets/new")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_wizard_renders_five_steps_prefilled_from_account(client, login, make_user):
    user = make_user(full_name="Jonalie Montera", email="jonalie@example.com")
    login(client, user)
    html = client.get("/lost-pets/new").get_data(as_text=True)
    for title in (
        "Pet Information",
        "Identifying Features",
        "Where &amp; When Lost",
        "Contact &amp; Reward",
        "Review and Submit",
    ):
        assert title in html
    assert html.count("data-wizard-step=") == 5
    assert "Step 1 of 5" in html
    assert 'value="Jonalie Montera"' in html
    assert 'value="jonalie@example.com"' in html
    assert 'name="photos"' in html and "multiple" in html
    assert "js/lost_pet.js" in html
    assert "<script>" not in html  # CSP: no inline scripts


# --- Create ---------------------------------------------------------------------------------


def test_create_report_saves_everything(client, login, user, upload, app):
    login(client, user)
    response = post_new(client, form_data(upload, photos=3))
    assert response.status_code == 302

    report = db.session.scalar(sa.select(LostReport))
    assert response.headers["Location"].endswith(f"/cases/{report.id}")
    assert CASE_RE.match(report.case_number)
    assert report.case_number == f"PB-{local_now().year}-000001"
    assert report.owner_id == user.id
    assert report.status == "lost"
    assert report.species == "dog" and report.size == "medium" and report.sex == "male"
    assert report.last_seen_place == "Brgy. San Jose, near the chapel"
    assert report.contact_phone == "+639175551234"
    assert report.contact_email == "maria@example.com"
    assert report.show_phone is False
    assert report.reward_amount == Decimal("1500.00")
    assert report.last_seen_at < utcnow()

    history = report.status_history
    assert len(history) == 1
    assert history[0].from_status is None and history[0].to_status == "lost"
    assert history[0].changed_by == user.id

    photos = report.photos
    assert len(photos) == 3
    assert photos[0].is_primary and not photos[1].is_primary and not photos[2].is_primary
    assert [p.sort_order for p in photos] == [0, 1, 2]
    assert all(p.public_id.startswith(f"lost_reports/{report.id}/") for p in photos)
    for photo in photos:
        assert (Path(app.config["UPLOAD_FOLDER"]) / photo.public_id).is_file()

    extract = jobs("extract_attributes")
    match = jobs("match_lost_report")
    assert [j.payload for j in extract] == [{"photo_id": photos[0].id}]
    assert [j.payload for j in match] == [{"lost_report_id": report.id}]

    audit = db.session.scalar(sa.select(AuditLog).where(AuditLog.event == "report.created"))
    assert audit is not None and audit.actor_id == user.id and audit.target_id == report.id


def test_create_flashes_case_number_on_details_page(client, login, user, upload):
    login(client, user)
    response = post_new(client, form_data(upload))
    html = client.get(response.headers["Location"]).get_data(as_text=True)
    report = db.session.scalar(sa.select(LostReport))
    assert f"Your case number is {report.case_number}" in html


def test_case_numbers_are_sequential(client, login, user, upload):
    login(client, user)
    post_new(client, form_data(upload))
    post_new(client, form_data(upload, pet_name="Muning", species="cat", breed="Puspin"))
    numbers = list(db.session.scalars(sa.select(LostReport.case_number).order_by(LostReport.id)))
    year = local_now().year
    assert numbers == [f"PB-{year}-000001", f"PB-{year}-000002"]


def test_contact_defaults_and_show_phone(client, login, make_user, upload):
    user = make_user(mobile_e164="+639181112222")
    login(client, user)
    post_new(client, form_data(upload, contact_phone="+63 918 111 2222", show_phone="1"))
    report = db.session.scalar(sa.select(LostReport))
    assert report.show_phone is True
    assert report.contact_phone == "+639181112222"


@pytest.mark.config(JOBS_EAGER=True)
def test_create_with_jobs_running_inline(client, login, user, upload, fake_gemini):
    """The whole flow also works when the background jobs run immediately (dev setup)."""
    login(client, user)
    response = post_new(client, form_data(upload))
    assert response.status_code == 302
    assert report_count() == 1
    assert {j.status for j in db.session.scalars(sa.select(Job))} <= {"done", "pending"}


# --- Validation --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "field_message"),
    [
        ({"species": "bird"}, "Invalid choice."),
        ({"species": ""}, "Please choose an option."),
        ({"pet_name": ""}, "This field is required."),
        ({"breed": "   "}, "This field is required."),
        ({"colors": ""}, "This field is required."),
        ({"size": "huge"}, "Invalid choice."),
        ({"sex": ""}, "Please choose an option."),
        ({"pet_name": "x" * 81}, "Must be at most 80 characters."),
        ({"lat": "", "lng": ""}, "Please pin a location"),
        ({"lat": "10.0", "lng": "124.0"}, "outside Tacloban City"),
        ({"lat": "abc", "lng": "125"}, "Please pin a location"),
        ({"last_seen_date": ""}, "Enter the date your pet was last seen."),
        ({"last_seen_date": "2026-13-45"}, "Enter a valid date."),
        ({"reward_amount": "-5"}, "The reward cannot be negative."),
        ({"reward_amount": "lots"}, "Enter the reward in pesos"),
        ({"reward_amount": "1e12"}, "That reward amount is too large."),
        ({"contact_name": ""}, "This field is required."),
        ({"contact_email": "not-an-email"}, "Enter a valid email address."),
        ({"contact_phone": "12345"}, "Enter a valid PH mobile number"),
        ({"contact_phone": "", "show_phone": "1"}, "Add a phone number"),
    ],
)
def test_create_validation_errors(client, login, user, upload, overrides, field_message):
    login(client, user)
    response = post_new(client, form_data(upload, **overrides))
    assert response.status_code == 400
    assert field_message in response.get_data(as_text=True)
    assert report_count() == 0
    assert db.session.scalar(sa.select(sa.func.count(Photo.id))) == 0
    assert jobs("match_lost_report") == []


def test_last_seen_in_future_is_rejected(client, login, user, upload):
    login(client, user)
    tomorrow = (local_now() + timedelta(days=1)).strftime("%Y-%m-%d")
    response = post_new(client, form_data(upload, last_seen_date=tomorrow))
    assert response.status_code == 400
    assert "cannot be in the future" in response.get_data(as_text=True)


@pytest.mark.config(OUT_OF_AREA_POLICY="warn")
def test_out_of_area_allowed_when_policy_is_warn(client, login, user, upload):
    login(client, user)
    response = post_new(client, form_data(upload, lat="10.0", lng="124.0"))
    assert response.status_code == 302


def test_error_reopens_the_right_wizard_step(client, login, user, upload):
    login(client, user)
    html = post_new(client, form_data(upload, lat="10", lng="124")).get_data(as_text=True)
    assert 'data-start-step="3"' in html
    assert 'value="Bantay"' in html  # typed values are kept
    assert "choose your photos again" in html


def test_photos_are_required(client, login, user, upload):
    login(client, user)
    response = post_new(client, form_data(upload, photos=0))
    assert response.status_code == 400
    assert "Add at least one clear photo" in response.get_data(as_text=True)
    assert report_count() == 0


def test_empty_file_part_counts_as_no_photo(client, login, user, upload):
    login(client, user)
    data = form_data(upload, photos=0)
    data["photos"] = (io.BytesIO(b""), "")
    assert post_new(client, data).status_code == 400
    assert report_count() == 0


def test_at_most_five_photos(client, login, user, upload):
    login(client, user)
    response = post_new(client, form_data(upload, photos=6))
    assert response.status_code == 400
    assert "up to 5 photos" in response.get_data(as_text=True)
    assert report_count() == 0


def test_invalid_image_rolls_everything_back(client, login, user, upload, app):
    login(client, user)
    data = form_data(upload, photos=1)
    data["photos"] = [upload("ok.jpg"), (io.BytesIO(b"this is not an image"), "fake.jpg")]
    response = post_new(client, data)
    assert response.status_code == 400
    assert "not a readable photo" in response.get_data(as_text=True)
    assert report_count() == 0
    assert db.session.scalar(sa.select(sa.func.count(Photo.id))) == 0
    assert db.session.scalar(sa.select(sa.func.count(CaseStatusHistory.id))) == 0
    folder = Path(app.config["UPLOAD_FOLDER"])
    assert not folder.exists() or not any(p.is_file() for p in folder.rglob("*"))
    # The case number was given back: the next report still gets 000001.
    post_new(client, form_data(upload))
    report = db.session.scalar(sa.select(LostReport))
    assert report.case_number.endswith("-000001")


def test_create_form_requires_csrf(csrf_client, login, user, upload):
    login(csrf_client, user)
    response = post_new(csrf_client, form_data(upload))
    assert response.status_code == 400
    assert report_count() == 0
    token = get_csrf_token(csrf_client, "/lost-pets/new")
    response = post_new(csrf_client, form_data(upload, csrf_token=token))
    assert response.status_code == 302
    assert report_count() == 1


@pytest.mark.config(RATELIMIT_ENABLED=True, RATE_LIMIT_REPORTS="1 per hour")
def test_create_is_rate_limited(client, login, user, upload):
    login(client, user)
    assert post_new(client, form_data(upload)).status_code == 302
    assert post_new(client, form_data(upload)).status_code == 429
    assert report_count() == 1


# --- Browse -------------------------------------------------------------------------------------


def test_browse_is_public_and_hides_hidden_and_non_lost(client, make_lost_report):
    make_lost_report(pet_name="Visible")
    make_lost_report(pet_name="Secret", is_hidden=True, hidden_reason="spam")
    make_lost_report(pet_name="HomeAgain", status="found")
    response = client.get("/lost-pets")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Visible" in html
    assert "Secret" not in html
    assert "HomeAgain" not in html
    found_html = client.get("/lost-pets?status=found").get_data(as_text=True)
    assert "HomeAgain" in found_html and "Visible" not in found_html
    all_html = client.get("/lost-pets?status=all").get_data(as_text=True)
    assert "HomeAgain" in all_html and "Visible" in all_html and "Secret" not in all_html


def test_browse_never_shows_phone_or_email(client, make_lost_report, make_user):
    owner = make_user(email="owner-private@example.com", mobile_e164="+639170000999")
    make_lost_report(owner=owner, show_phone=False)
    html = client.get("/lost-pets").get_data(as_text=True)
    assert "owner-private@example.com" not in html
    assert "0000999" not in html
    assert owner.full_name in html  # owner name appears on cards (SPEC 17.5)


def test_browse_filters_species_and_text(client, make_lost_report):
    make_lost_report(pet_name="Bantay", species="dog", breed="Aspin", last_seen_place="Brgy. 77")
    make_lost_report(pet_name="Muning", species="cat", breed="Persian", colors="Calico")
    assert "Muning" not in client.get("/lost-pets?species=dog").get_data(as_text=True)
    assert "Bantay" not in client.get("/lost-pets?species=cat").get_data(as_text=True)
    html = client.get("/lost-pets?q=persian").get_data(as_text=True)
    assert "Muning" in html and "Bantay" not in html
    html = client.get("/lost-pets?q=calico").get_data(as_text=True)
    assert "Muning" in html
    html = client.get("/lost-pets?q=brgy. 77").get_data(as_text=True)
    assert "Bantay" in html and "Muning" not in html
    html = client.get("/lost-pets?q=100%25").get_data(as_text=True)  # LIKE wildcards escaped
    assert "No cases match your search" in html


def test_browse_near_me_sorts_by_distance_and_filters_radius(client, make_lost_report):
    lat, lng = TACLOBAN
    make_lost_report(pet_name="Far", last_seen_lat=lat + 0.03, last_seen_lng=lng)  # ~3.3 km
    make_lost_report(pet_name="Near", last_seen_lat=lat + 0.001, last_seen_lng=lng)  # ~0.1 km
    make_lost_report(pet_name="Outside", last_seen_lat=lat + 0.12, last_seen_lng=lng)  # ~13 km
    html = client.get(f"/lost-pets?near={lat},{lng}&radius=5").get_data(as_text=True)
    assert html.index("Near") < html.index("Far")
    assert "Outside" not in html
    assert "0.1 km away" in html


def test_browse_pagination(client, make_lost_report):
    for i in range(13):
        make_lost_report(pet_name=f"Pet{i:02d}")
    page1 = client.get("/lost-pets").get_data(as_text=True)
    page2 = client.get("/lost-pets?page=2").get_data(as_text=True)
    assert "page 1 of 2" in page1 and "Pet12" in page1 and "Pet00" not in page1
    assert "Pet00" in page2 and "Pet12" not in page2
    assert 'rel="next"' in page1 and 'rel="prev"' in page2


def test_browse_guest_message_button_and_owner_has_none(client, login, make_lost_report):
    report = make_lost_report()
    html = client.get("/lost-pets").get_data(as_text=True)
    assert f"/messages/new?lost_report_id={report.id}" in html
    login(client, report.owner)
    html = client.get("/lost-pets").get_data(as_text=True)
    assert f"/messages/new?lost_report_id={report.id}" not in html


def test_browse_empty_state(client):
    html = client.get("/lost-pets").get_data(as_text=True)
    assert "No lost pets reported right now" in html


# --- JSON API -------------------------------------------------------------------------------------


def test_api_list_shape_and_privacy(client, make_lost_report, make_user):
    owner = make_user(email="hide-me@example.com")
    report = make_lost_report(owner=owner, reward_amount=Decimal("500"))
    make_lost_report(pet_name="Hidden", is_hidden=True)
    response = client.get("/api/lost-reports")
    assert response.status_code == 200
    body = response.get_json()
    assert body["total"] == 1 and body["page"] == 1 and body["pages"] == 1
    item = body["items"][0]
    assert item["id"] == report.id
    assert item["case_number"] == report.case_number
    assert item["status"] == "lost"
    assert item["last_seen"]["lat"] == pytest.approx(TACLOBAN[0])
    assert item["last_seen"]["at"].endswith("Z")
    assert item["reward"]["amount"] == 500.0
    assert item["url"] == f"/cases/{report.id}"
    assert item["thumb_url"].startswith("/media/")
    assert "contact" not in item
    assert "hide-me@example.com" not in response.get_data(as_text=True)
    assert owner.mobile_e164 not in response.get_data(as_text=True)


def test_api_list_near_and_filters(client, make_lost_report):
    lat, lng = TACLOBAN
    far = make_lost_report(pet_name="Far", last_seen_lat=lat + 0.02, last_seen_lng=lng)
    near = make_lost_report(pet_name="Near", last_seen_lat=lat, last_seen_lng=lng + 0.001)
    make_lost_report(pet_name="Cat", species="cat", last_seen_lat=lat, last_seen_lng=lng)
    body = client.get(f"/api/lost-reports?near={lat},{lng}&species=dog").get_json()
    assert [i["id"] for i in body["items"]] == [near.id, far.id]
    assert body["items"][0]["distance_km"] < body["items"][1]["distance_km"]
    body = client.get("/api/lost-reports?q=cat").get_json()
    assert [i["pet_name"] for i in body["items"]] == ["Cat"]
    body = client.get("/api/lost-reports?per_page=1&page=2").get_json()
    assert body["per_page"] == 1 and body["pages"] == 3 and len(body["items"]) == 1


@pytest.mark.parametrize("query", ["near=abc", "near=91,200", "species=bird", "status=whatever"])
def test_api_list_rejects_bad_params(client, query):
    response = client.get(f"/api/lost-reports?{query}")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "validation_error"


def test_api_create(client, login, user, upload):
    login(client, user)
    response = client.post(
        "/api/lost-reports", data=form_data(upload, photos=2), content_type="multipart/form-data"
    )
    assert response.status_code == 201
    body = response.get_json()
    report = db.session.get(LostReport, body["id"])
    assert body == {
        "id": report.id,
        "case_number": report.case_number,
        "url": f"/cases/{report.id}",
    }
    assert len(report.photos) == 2
    assert len(jobs("match_lost_report")) == 1


def test_api_create_requires_login(client, upload):
    response = client.post(
        "/api/lost-reports", data=form_data(upload), content_type="multipart/form-data"
    )
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "auth_required"


def test_api_create_validation_details(client, login, user, upload):
    login(client, user)
    response = client.post(
        "/api/lost-reports",
        data=form_data(upload, photos=0, species="bird", lat="1", lng="1"),
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == "validation_error"
    assert set(error["details"]) >= {"species", "location", "photos"}


def test_api_create_bad_image(client, login, user, upload):
    login(client, user)
    data = form_data(upload, photos=0)
    data["photos"] = (io.BytesIO(b"GIF89a-not-really"), "cat.gif")
    response = client.post("/api/lost-reports", data=data, content_type="multipart/form-data")
    assert response.status_code == 400
    assert "photos" in response.get_json()["error"]["details"]
    assert report_count() == 0


def test_api_create_requires_csrf_header(csrf_client, login, user, upload):
    login(csrf_client, user)
    response = csrf_client.post(
        "/api/lost-reports", data=form_data(upload), content_type="multipart/form-data"
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "csrf_failed"
    token = get_csrf_token(csrf_client)
    response = csrf_client.post(
        "/api/lost-reports",
        data=form_data(upload),
        content_type="multipart/form-data",
        headers={"X-CSRFToken": token},
    )
    assert response.status_code == 201


def test_api_detail_public_masks_contact(client, make_lost_report, make_user):
    owner = make_user(mobile_e164="+639171234567", email="owner@example.com")
    report = make_lost_report(owner=owner, photos=2)
    body = client.get(f"/api/lost-reports/{report.id}").get_json()
    assert body["contact"]["phone"] == "+63 9** *** 4567"
    assert body["contact"]["phone_masked"] is True
    assert body["contact"]["tel"] is None
    assert body["contact"]["email"] is None
    assert len(body["photos"]) == 2 and body["photos"][0]["is_primary"] is True
    assert body["history"][0]["to_status"] == "lost"
    assert body["maps"]["view_url"].startswith("https://www.google.com/maps/search/?api=1")
    assert body["can_manage"] is False
    assert "is_hidden" not in body
    assert "+639171234567" not in str(body)


def test_api_detail_show_phone_and_owner_view(client, login, make_lost_report, make_user):
    owner = make_user(mobile_e164="+639171234567", email="owner@example.com")
    report = make_lost_report(owner=owner, show_phone=True)
    body = client.get(f"/api/lost-reports/{report.id}").get_json()
    assert body["contact"]["phone"] == "+63 917 123 4567"
    assert body["contact"]["tel"] == "tel:+639171234567"
    login(client, owner)
    body = client.get(f"/api/lost-reports/{report.id}").get_json()
    assert body["contact"]["email"] == "owner@example.com"
    assert body["can_manage"] is True and body["is_hidden"] is False


def test_api_detail_hidden_report(client, login, make_lost_report, make_user, admin):
    report = make_lost_report(is_hidden=True, hidden_reason="spam")
    assert client.get(f"/api/lost-reports/{report.id}").status_code == 404
    login(client, make_user())
    response = client.get(f"/api/lost-reports/{report.id}")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"
    login(client, report.owner)
    assert client.get(f"/api/lost-reports/{report.id}").get_json()["is_hidden"] is True
    login(client, admin)
    assert client.get(f"/api/lost-reports/{report.id}").status_code == 200


def test_api_detail_missing(client):
    response = client.get("/api/lost-reports/999")
    assert response.status_code == 404
    assert response.get_json() == {
        "error": {"code": "not_found", "message": response.get_json()["error"]["message"]}
    }


# --- Edit ---------------------------------------------------------------------------------------------


def edit_data(report, upload=None, **overrides) -> dict:
    local = report.last_seen_at + timedelta(hours=8)
    data = {
        "species": report.species,
        "pet_name": report.pet_name,
        "breed": report.breed,
        "colors": report.colors,
        "size": report.size,
        "sex": report.sex,
        "lat": str(report.last_seen_lat),
        "lng": str(report.last_seen_lng),
        "place": report.last_seen_place or "",
        "last_seen_date": local.strftime("%Y-%m-%d"),
        "last_seen_time": local.strftime("%H:%M"),
        "contact_name": report.contact_name,
        "contact_email": report.contact_email or "",
        "contact_phone": report.contact_phone or "",
    }
    data.update(overrides)
    return data


def post_edit(client, report, data):
    return client.post(
        f"/lost-pets/{report.id}/edit", data=data, content_type="multipart/form-data"
    )


def test_edit_access_rules(client, login, make_lost_report, make_user, admin):
    report = make_lost_report()
    assert client.get(f"/lost-pets/{report.id}/edit").status_code == 302  # guest -> login
    login(client, make_user())
    assert client.get(f"/lost-pets/{report.id}/edit").status_code == 403
    assert post_edit(client, report, edit_data(report, pet_name="Hacked")).status_code == 403
    assert report.pet_name == "Bantay"
    login(client, report.owner)
    html = client.get(f"/lost-pets/{report.id}/edit").get_data(as_text=True)
    assert 'value="Bantay"' in html and report.case_number in html
    login(client, admin)
    assert client.get(f"/lost-pets/{report.id}/edit").status_code == 200
    assert client.get("/lost-pets/999/edit").status_code == 404


def test_edit_hidden_report_is_404_for_others(client, login, make_lost_report, make_user):
    report = make_lost_report(is_hidden=True)
    login(client, make_user())
    assert client.get(f"/lost-pets/{report.id}/edit").status_code == 404
    login(client, report.owner)
    assert client.get(f"/lost-pets/{report.id}/edit").status_code == 200


def test_edit_updates_fields_and_rematches(client, login, make_lost_report):
    report = make_lost_report(photos=2)
    case_number = report.case_number
    login(client, report.owner)
    response = post_edit(
        client, report, edit_data(report, pet_name="Bantay Jr", colors="Black", show_phone="1")
    )
    assert response.status_code == 302
    db.session.refresh(report)
    assert report.pet_name == "Bantay Jr" and report.colors == "Black"
    assert report.show_phone is True
    assert report.case_number == case_number
    assert len(report.status_history) == 1
    assert [j.payload for j in jobs("match_lost_report")] == [{"lost_report_id": report.id}]
    assert jobs("extract_attributes") == []  # the main photo did not change
    assert db.session.scalar(sa.select(AuditLog).where(AuditLog.event == "report.updated"))


def test_edit_without_match_changes_does_not_rematch(client, login, make_lost_report):
    report = make_lost_report()
    login(client, report.owner)
    post_edit(client, report, edit_data(report, pet_name="New name", reward_text="Thanks!"))
    assert jobs("match_lost_report") == []


def test_edit_photos_remove_add_and_choose_main(client, login, make_lost_report, upload):
    report = make_lost_report(photos=2, ai_attributes={"species": "dog"})
    first, second = report.photos
    login(client, report.owner)
    data = edit_data(report, primary_photo_id=str(second.id), remove_photo=[str(first.id)])
    data["photos"] = [upload("new.jpg")]
    response = post_edit(client, report, data)
    assert response.status_code == 302
    photos = Photo.for_owner(PhotoOwner.LOST_REPORT, report.id)
    assert [p.id for p in photos][0] == second.id
    assert len(photos) == 2 and first.id not in [p.id for p in photos]
    assert photos[0].is_primary and not photos[1].is_primary
    assert db.session.get(LostReport, report.id).ai_attributes is None
    assert [j.payload for j in jobs("extract_attributes")] == [{"photo_id": second.id}]
    assert len(jobs("match_lost_report")) == 1


def test_edit_cannot_remove_every_photo(client, login, make_lost_report):
    report = make_lost_report(photos=1)
    login(client, report.owner)
    only = report.photos[0]
    response = post_edit(client, report, edit_data(report, remove_photo=[str(only.id)]))
    assert response.status_code == 400
    assert "Add at least one clear photo" in response.get_data(as_text=True)
    assert len(report.photos) == 1


def test_edit_ignores_photo_ids_of_other_reports(client, login, make_lost_report):
    report = make_lost_report(photos=1)
    other = make_lost_report(photos=1)
    login(client, report.owner)
    other_photo = other.photos[0]
    response = post_edit(client, report, edit_data(report, remove_photo=[str(other_photo.id)]))
    assert response.status_code == 302
    assert db.session.get(Photo, other_photo.id) is not None


def test_edit_validation_error_keeps_values(client, login, make_lost_report):
    report = make_lost_report()
    login(client, report.owner)
    response = post_edit(client, report, edit_data(report, pet_name="", colors="Grey"))
    assert response.status_code == 400
    assert 'value="Grey"' in response.get_data(as_text=True)
    assert db.session.get(LostReport, report.id).pet_name == "Bantay"


def test_closed_case_cannot_be_edited(client, login, make_lost_report):
    report = make_lost_report(status="closed")
    login(client, report.owner)
    response = client.get(f"/lost-pets/{report.id}/edit")
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/cases/{report.id}")
