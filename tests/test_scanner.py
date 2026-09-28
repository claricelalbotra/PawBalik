"""Pet Scanner pages and API (SPEC 5.2, 5.3, 16.4-16.6).

Most tests replace the matching engine with ``FakeEngine`` (a stand-in for
``matching_service.run_scan`` / ``results_for_display``) so the scanner's own
behaviour is tested independently: upload handling, rejections, privacy
filtering, access logging and rendering. One test runs the real engine with
the ``fake_gemini`` fixture.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from conftest import TACLOBAN, jpeg_bytes

from app.extensions import db
from app.models import (
    AuditLog,
    CandidateType,
    Photo,
    PhotoOwner,
    Scan,
    ScanResult,
    ScanStatus,
    Sighting,
    SightingAccessLog,
)
from app.services import matching_service
from app.utils import strings
from app.utils.cards import lost_report_card, sighting_card

TIERS = ((80, "high"), (60, "moderate"), (40, "low"))


def tier_for(score: float) -> str:
    for minimum, name in TIERS:
        if score >= minimum:
            return name
    return "hidden"


class FakeEngine:
    """Deterministic run_scan/results_for_display used by the scanner tests.

    ``candidates``: list of (LostReport | Sighting, score) to put in every scan.
    ``reject``: a ScanRejected to raise instead. ``calls``: kwargs of each run_scan.
    """

    def __init__(self) -> None:
        self.candidates: list[tuple[Any, float]] = []
        self.reject: matching_service.ScanRejected | None = None
        self.keep_rejected_scan = False
        self.precheck: dict = {"species": "dog", "image_quality": "good", "animal_count": 1}
        self.attributes: dict = {
            "species": "dog",
            "breed_guess": "Aspin (mixed)",
            "primary_colors": ["brown", "white"],
            "coat_pattern": "bicolor",
            "size_estimate": "medium",
        }
        self.gemini_ok = True
        self.visual_unavailable = False
        self.calls: list[dict] = []

    def run_scan(self, **kwargs: Any) -> Scan:
        self.calls.append(kwargs)
        user, photo = kwargs["user"], kwargs["photo"]
        if self.reject is not None:
            if self.keep_rejected_scan:
                db.session.add(
                    Scan(
                        user_id=user.id,
                        mode=kwargs["mode"],
                        query_photo_id=photo.id,
                        status=ScanStatus.REJECTED,
                    )
                )
                db.session.flush()
            raise self.reject
        report = kwargs.get("source_lost_report")
        scan = Scan(
            user_id=user.id,
            mode=kwargs["mode"],
            trigger=kwargs.get("trigger") or "manual",
            query_photo_id=photo.id,
            source_lost_report_id=report.id if report is not None else None,
            scan_lat=kwargs.get("lat"),
            scan_lng=kwargs.get("lng"),
            precheck=dict(self.precheck),
            attributes=dict(self.attributes),
            status=ScanStatus.COMPLETE,
            gemini_ok=self.gemini_ok,
        )
        db.session.add(scan)
        db.session.flush()
        for rank, (candidate, score) in enumerate(self.candidates, start=1):
            is_sighting = isinstance(candidate, Sighting)
            db.session.add(
                ScanResult(
                    scan_id=scan.id,
                    candidate_type=(
                        CandidateType.SIGHTING if is_sighting else CandidateType.LOST_REPORT
                    ),
                    candidate_id=candidate.id,
                    candidate_photo_id=(
                        candidate.primary_photo.id if candidate.primary_photo else None
                    ),
                    visual_score=score,
                    attribute_score=60,
                    distance_km=0.6,
                    final_score=score,
                    tier=tier_for(score),
                    rank=rank,
                    visual_check_unavailable=self.visual_unavailable,
                    explanation={
                        "matching_features": ["similar white chest blaze"],
                        "differences": ["collar colour differs"],
                        "confidence": "medium",
                    },
                )
            )
        scan.result_count = len(self.candidates)
        db.session.flush()
        return scan

    def results_for_display(self, scan: Scan, include_weak: bool = False) -> list[dict]:
        rows = []
        for r in scan.results:
            if r.tier == "hidden" and not include_weak:
                continue
            kwargs = dict(
                distance_km=r.distance_km,
                similarity=r.final_score,
                tier=r.tier,
                explanation=r.explanation,
                visual_check_unavailable=r.visual_check_unavailable,
            )
            if r.candidate_type == CandidateType.LOST_REPORT:
                card = lost_report_card(r.candidate, **kwargs)
            else:
                card = sighting_card(r.candidate, **kwargs)
            card.update(
                result_id=r.id,
                candidate_type=r.candidate_type,
                candidate_id=r.candidate_id,
                final_score=r.final_score,
            )
            rows.append(card)
        return rows


@pytest.fixture
def engine(monkeypatch) -> FakeEngine:
    fake = FakeEngine()
    monkeypatch.setattr(matching_service, "run_scan", fake.run_scan)
    monkeypatch.setattr(matching_service, "results_for_display", fake.results_for_display)
    return fake


def post_scan(
    client, *, mode="found", photo=True, color=(200, 140, 80), url="/api/scans", **fields
):
    data: dict[str, Any] = {"mode": mode, **fields}
    if photo:
        data["photo"] = (io.BytesIO(jpeg_bytes(color)), "pet.jpg")
    return client.post(url, data=data, content_type="multipart/form-data")


# --- Access control --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/scan", "/scan/found", "/scan/lost"])
def test_scanner_pages_require_login(client, path):
    response = client.get(path)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_scan_api_requires_login(client):
    response = post_scan(client)
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "auth_required"


# --- Choose pages ------------------------------------------------------------------------------


def test_scan_index_offers_both_scanners(client, user, login):
    login(client, user)
    html = client.get("/scan").get_data(as_text=True)
    assert "Match a Found Pet" in html and "Scan for My Lost Pet" in html
    assert 'href="/scan/found"' in html and 'href="/scan/lost"' in html


def test_found_scanner_page(client, user, login):
    login(client, user)
    response = client.get("/scan/found")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Found Pet" in html
    assert "Upload Photo" in html and "Capture Photo" in html
    assert 'capture="environment"' in html
    assert strings.SCAN_TIP in html
    assert 'action="/scan"' in html and 'enctype="multipart/form-data"' in html
    assert "js/scanner.js" in html
    assert "data-scan-progress" in html  # the scanning state (scanner/scan.html) is included
    assert "<script>" not in html  # CSP: no inline scripts


def test_lost_scanner_lists_only_own_active_reports(
    client, user, make_user, make_lost_report, login
):
    mine = make_lost_report(owner=user, pet_name="Bantay")
    make_lost_report(owner=user, pet_name="ClosedPet", status="closed")
    make_lost_report(owner=make_user(), pet_name="SomeoneElse")
    login(client, user)
    html = client.get(f"/scan/lost?lost_report_id={mine.id}").get_data(as_text=True)
    assert "Scan for" in html and "My Lost Pet" in html
    assert "Bantay" in html and mine.case_number in html
    assert "ClosedPet" not in html and "SomeoneElse" not in html
    assert f'value="{mine.id}" data-has-photo="1" checked' in html


def test_lost_scanner_without_reports_suggests_filing_one(client, user, login):
    login(client, user)
    html = client.get("/scan/lost").get_data(as_text=True)
    assert "You have no active lost pet reports" in html
    assert "Upload Photo" in html


# --- POST /api/scans ------------------------------------------------------------------------


def test_found_scan_happy_path(client, user, make_lost_report, engine, login):
    report = make_lost_report(pet_name="Bantay")
    engine.candidates = [(report, 84.0)]
    login(client, user)

    response = post_scan(client, lat=str(TACLOBAN[0]), lng=str(TACLOBAN[1]))

    assert response.status_code == 201
    body = response.get_json()
    scan = db.session.get(Scan, body["scan_id"])
    assert scan.user_id == user.id and scan.mode == "found"
    assert body["url"] == f"/scans/{scan.id}"
    assert body["weak_count"] == 0 and body["warnings"] == []
    assert [r["candidate_id"] for r in body["results"]] == [report.id]
    assert body["results"][0]["similarity"] == 84
    assert body["results"][0]["candidate_type"] == "lost_report"
    # The upload was stored as a scan photo and attached to the scan.
    photo = scan.query_photo
    assert photo.owner_type == PhotoOwner.SCAN and photo.owner_id == scan.id
    call = engine.calls[0]
    assert call["mode"] == "found" and call["user"].id == user.id
    assert call["lat"] == pytest.approx(TACLOBAN[0]) and call["trigger"] == "manual"


def test_found_scan_never_returns_sightings(
    client, user, make_lost_report, make_sighting, engine, login
):
    """Defence in depth: even if a found-mode scan held a sighting result, it is not shown."""
    report = make_lost_report(pet_name="Bantay")
    sighting = make_sighting(seen_place="Secret spot near the pier")
    engine.candidates = [(report, 70.0), (sighting, 95.0)]
    login(client, user)

    body = post_scan(client).get_json()
    assert [r["candidate_type"] for r in body["results"]] == ["lost_report"]
    assert "Secret spot" not in str(body)

    html = client.get(f"/scans/{body['scan_id']}?weak=1").get_data(as_text=True)
    assert "Secret spot" not in html
    assert f"/sightings/{sighting.id}" not in html
    assert db.session.scalar(sa.select(sa.func.count(SightingAccessLog.id))) == 0


def test_scan_rejection_returns_friendly_422_and_deletes_upload(client, user, engine, login, app):
    engine.reject = matching_service.ScanRejected(
        "not_dog_or_cat",
        "We could not find a dog or cat in this photo.",
        {"species": "none"},
    )
    login(client, user)

    response = post_scan(client)

    assert response.status_code == 422
    error = response.get_json()["error"]
    assert error["code"] == "not_dog_or_cat"
    assert error["message"] == "We could not find a dog or cat in this photo."
    assert error["details"] == {"species": "none"}
    assert db.session.scalar(sa.select(sa.func.count(Photo.id))) == 0
    files = [p for p in Path(app.config["UPLOAD_FOLDER"]).rglob("*") if p.is_file()]
    assert files == []


def test_rejected_scan_row_is_kept_but_its_photo_deleted(client, user, engine, login):
    engine.reject = matching_service.ScanRejected("multiple_animals", "One animal per photo.")
    engine.keep_rejected_scan = True
    login(client, user)
    assert post_scan(client).status_code == 422
    scan = db.session.scalar(sa.select(Scan))
    assert scan.status == ScanStatus.REJECTED
    assert scan.query_photo_id is None
    assert db.session.scalar(sa.select(sa.func.count(Photo.id))) == 0


def test_scan_validation_errors(client, user, engine, login):
    login(client, user)
    missing = post_scan(client, photo=False)
    assert missing.status_code == 422
    assert missing.get_json()["error"]["code"] == "validation_error"

    bad_mode = post_scan(client, mode="everything")
    assert bad_mode.status_code == 422

    not_image = client.post(
        "/api/scans",
        data={"mode": "found", "photo": (io.BytesIO(b"not an image at all"), "x.jpg")},
        content_type="multipart/form-data",
    )
    assert not_image.status_code == 422
    assert not_image.get_json()["error"]["code"] == "invalid_image"
    assert engine.calls == []


def test_lost_scan_with_own_report_uses_report_photo(
    client, user, make_lost_report, make_sighting, engine, login
):
    report = make_lost_report(owner=user)
    engine.candidates = [(make_sighting(), 81.0)]
    login(client, user)

    response = post_scan(client, mode="lost", photo=False, lost_report_id=str(report.id))

    assert response.status_code == 201
    call = engine.calls[0]
    assert call["photo"].id == report.primary_photo.id
    assert call["source_lost_report"].id == report.id
    # Ranked from the last-seen location when the user shares none.
    assert (call["lat"], call["lng"]) == (report.last_seen_lat, report.last_seen_lng)
    # The report's photo keeps belonging to the report.
    assert report.primary_photo.owner_type == PhotoOwner.LOST_REPORT


def test_scan_with_report_photo_button_ignores_chosen_file(
    client, user, make_lost_report, engine, login
):
    report = make_lost_report(owner=user)
    login(client, user)
    post_scan(client, mode="lost", lost_report_id=str(report.id), use_report_photo="1")
    assert engine.calls[0]["photo"].id == report.primary_photo.id


def test_lost_scan_rejects_someone_elses_report(
    client, user, make_user, make_lost_report, engine, login
):
    other_report = make_lost_report(owner=make_user())
    login(client, user)
    response = post_scan(client, mode="lost", photo=False, lost_report_id=str(other_report.id))
    assert response.status_code == 422
    assert engine.calls == []


def test_location_outside_tacloban_is_ignored_with_warning(client, user, engine, login):
    login(client, user)
    body = post_scan(client, lat="14.5995", lng="120.9842").get_json()  # Manila
    assert engine.calls[0]["lat"] is None and engine.calls[0]["lng"] is None
    assert any("outside Tacloban" in w for w in body["warnings"])


def test_scan_api_is_rate_limited_by_default_config(app):
    assert app.config["RATE_LIMIT_SCANS"] == "20 per hour"


@pytest.mark.config(RATELIMIT_ENABLED=True, RATE_LIMIT_SCANS="2 per hour")
def test_scan_api_rate_limit(client, user, engine, login):
    login(client, user)
    assert post_scan(client).status_code == 201
    assert post_scan(client).status_code == 201
    limited = post_scan(client)
    assert limited.status_code == 429
    assert limited.get_json()["error"]["code"] == "rate_limited"
    # The no-JS form shares the same budget.
    assert post_scan(client, url="/scan").status_code == 429


# --- Results page ------------------------------------------------------------------------------


def _scan_for(client, user, login, engine, candidates, **fields) -> int:
    engine.candidates = candidates
    login(client, user)
    response = post_scan(client, **fields)
    assert response.status_code == 201, response.get_json()
    return response.get_json()["scan_id"]


def test_found_results_page(client, user, make_lost_report, engine, login):
    strong = make_lost_report(pet_name="Mochi", breed="Pomeranian", colors="White")
    weak = make_lost_report(pet_name="Bruno")
    scan_id = _scan_for(client, user, login, engine, [(strong, 84.0), (weak, 22.0)])

    html = client.get(f"/scans/{scan_id}").get_data(as_text=True)

    assert "We found" in html and "1 possible match" in html
    assert "Your scanned photo" in html and "Scan Complete" in html
    assert strings.NONE_LOOK_RIGHT_TITLE in html
    assert strings.REPORT_AS_SIGHTING in html
    assert strings.DISCLAIMER in html
    assert "84% match" in html and "High visual similarity" in html
    assert "Mochi" in html and "Lost 2 days ago" in html
    assert "Pomeranian · White · Male" in html
    assert "Owner: " in html and "View Details" in html
    assert f"/messages/new?lost_report_id={strong.id}" in html
    assert "Why similar?" in html and "similar white chest blaze" in html
    assert "Bruno" not in html  # weak result hidden by default
    assert "Show weaker results (1)" in html
    assert 'id="sighting-modal"' in html and 'data-open-modal="sighting-modal"' in html

    weak_html = client.get(f"/scans/{scan_id}?weak=1").get_data(as_text=True)
    assert "Bruno" in weak_html and "Weaker results" in weak_html


def test_found_results_without_matches_offers_sighting(client, user, engine, login):
    scan_id = _scan_for(client, user, login, engine, [])
    html = client.get(f"/scans/{scan_id}").get_data(as_text=True)
    assert "No similar lost-pet reports" in html
    assert "the owner may not have reported yet" in html
    assert "Submit as Pet Sighting" in html
    assert f"/sightings/new?scan_id={scan_id}" in html
    assert strings.DISCLAIMER in html


def test_results_show_quality_and_fallback_warnings(client, user, make_lost_report, engine, login):
    engine.precheck = {"species": "dog", "image_quality": "poor", "quality_issues": ["blurry"]}
    engine.gemini_ok = False
    engine.visual_unavailable = True
    scan_id = _scan_for(client, user, login, engine, [(make_lost_report(), 70.0)])
    html = client.get(f"/scans/{scan_id}").get_data(as_text=True)
    assert "photo quality looks poor (blurry)" in html
    assert "Photo comparison was unavailable" in html
    assert "Moderate visual similarity" in html


def test_results_are_owner_only(client, user, make_user, make_admin, engine, login):
    scan_id = _scan_for(client, user, login, engine, [])
    other = make_user()
    login(client, other)
    assert client.get(f"/scans/{scan_id}").status_code == 404
    api = client.get(f"/api/scans/{scan_id}")
    assert api.status_code == 404 and api.get_json()["error"]["code"] == "not_found"

    login(client, make_admin())
    assert client.get(f"/scans/{scan_id}").status_code == 200

    with client.session_transaction() as session:
        session.clear()
    assert client.get(f"/scans/{scan_id}").status_code == 302


def test_scan_api_detail(client, user, make_lost_report, engine, login):
    report = make_lost_report()
    scan_id = _scan_for(client, user, login, engine, [(report, 90.0)])
    body = client.get(f"/api/scans/{scan_id}").get_json()
    assert body["scan"]["id"] == scan_id and body["scan"]["mode"] == "found"
    assert body["scan"]["photo_url"].startswith("/media/scans/")
    assert [r["candidate_id"] for r in body["results"]] == [report.id]


def test_rejected_scan_results_page(client, user, login):
    scan = Scan(user_id=user.id, mode="found", status=ScanStatus.REJECTED)
    db.session.add(scan)
    db.session.commit()
    login(client, user)
    html = client.get(f"/scans/{scan.id}").get_data(as_text=True)
    assert "This scan did not finish" in html


# --- Lost mode: sighting results and access logging -------------------------------------------


def test_lost_results_show_sighting_details_and_log_access(
    client, user, make_user, make_lost_report, make_sighting, engine, login
):
    report = make_lost_report(owner=user)
    finder = make_user(full_name="Claricel Finder")
    in_app = make_sighting(finder=finder, seen_place="Near the Astrodome", contact_pref="in_app")
    by_phone = make_sighting(
        finder=make_user(full_name="Jonalie Finder"),
        seen_place="Magsaysay Blvd",
        contact_pref="phone",
        contact_phone="+639171234567",
    )
    scan_id = _scan_for(
        client,
        user,
        login,
        engine,
        [(in_app, 88.0), (by_phone, 65.0)],
        mode="lost",
        lost_report_id=str(report.id),
    )
    access_after_api = db.session.scalar(sa.select(sa.func.count(SightingAccessLog.id)))
    assert access_after_api == 2  # the API response showed both sightings

    html = client.get(f"/scans/{scan_id}").get_data(as_text=True)

    assert "2 possible matches" in html
    assert "Near the Astrodome" in html and "Magsaysay Blvd" in html
    assert "Finder: Claricel Finder" in html
    assert in_app.contact_phone not in html  # finder chose in-app messages only
    assert "tel:+639171234567" in html  # finder chose phone
    assert "Prefers in-app messages" in html
    assert "https://www.google.com/maps/search/?api=1&amp;query=" in html
    assert "https://www.google.com/maps/dir/?api=1&amp;destination=" in html
    assert f"sighting_id={in_app.id}" in html and f"lost_report_id={report.id}" in html
    assert "Message finder" in html
    assert strings.DISCLAIMER in html
    assert strings.NONE_LOOK_RIGHT_TITLE not in html  # found-mode callout only
    assert 'id="sighting-modal"' not in html

    logs = db.session.scalars(sa.select(SightingAccessLog)).all()
    assert len(logs) == 4
    assert {log.viewer_id for log in logs} == {user.id}
    assert {log.scan_id for log in logs} == {scan_id}
    audit = db.session.scalars(sa.select(AuditLog).where(AuditLog.event == "sighting.access")).all()
    assert len(audit) == 4


def test_lost_results_drop_sightings_hidden_later(client, user, make_sighting, engine, login):
    sighting = make_sighting(seen_place="Hidden place")
    scan_id = _scan_for(client, user, login, engine, [(sighting, 90.0)], mode="lost")
    sighting.is_hidden = True
    db.session.commit()
    html = client.get(f"/scans/{scan_id}").get_data(as_text=True)
    assert "Hidden place" not in html
    body = client.get(f"/api/scans/{scan_id}").get_json()
    assert body["results"] == []


# --- No-JS form --------------------------------------------------------------------------------


def test_no_js_submit_redirects_to_results(client, user, make_lost_report, engine, login):
    engine.candidates = [(make_lost_report(), 90.0)]
    login(client, user)
    response = post_scan(client, url="/scan")
    assert response.status_code == 302
    scan = db.session.scalar(sa.select(Scan))
    assert response.headers["Location"].endswith(f"/scans/{scan.id}")


def test_no_js_submit_shows_errors(client, user, engine, login):
    engine.reject = matching_service.ScanRejected("not_dog_or_cat", "Only dogs and cats, please.")
    login(client, user)
    response = post_scan(client, url="/scan")
    assert response.status_code == 422
    html = response.get_data(as_text=True)
    assert "Only dogs and cats, please." in html and "Upload Photo" in html

    missing = post_scan(client, url="/scan", photo=False)
    assert missing.status_code == 422
    assert "Please upload or capture a photo" in missing.get_data(as_text=True)


def test_scan_form_requires_csrf(csrf_client, user, engine, login):
    login(csrf_client, user)
    assert post_scan(csrf_client).status_code == 400


# --- Real engine (runs once the engine team's pipeline exists) --------------------------------


def test_scan_api_with_real_engine_and_fake_gemini(
    client, user, make_lost_report, fake_gemini, login
):
    report = make_lost_report(pet_name="Bantay")
    fake_gemini.compare_score = 90
    login(client, user)
    try:
        response = post_scan(client)
    except NotImplementedError:
        pytest.skip("matching engine not built yet")
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    ids = [(r["candidate_type"], r["candidate_id"]) for r in body["results"]]
    assert ("lost_report", report.id) in ids
    assert all(t == "lost_report" for t, _ in ids)
    assert client.get(body["url"]).status_code == 200


def test_real_engine_rejection_deletes_the_upload(client, user, fake_gemini, login):
    fake_gemini.precheck_result = {
        "contains_animal": True,
        "species": "other",
        "animal_count": 1,
        "image_quality": "good",
        "quality_issues": [],
        "is_real_photo": True,
    }
    login(client, user)
    response = post_scan(client)
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "not_dog_or_cat"
    scan = db.session.scalar(sa.select(Scan))
    assert scan.status == ScanStatus.REJECTED and scan.query_photo_id is None
    assert db.session.scalar(sa.select(sa.func.count(Photo.id))) == 0


def test_real_engine_lost_mode_finds_sighting_and_logs_access(
    client, user, make_lost_report, make_sighting, fake_gemini, login
):
    report = make_lost_report(owner=user)
    sighting = make_sighting(seen_place="Near San Jose church")
    fake_gemini.compare_score = 92
    login(client, user)

    response = post_scan(client, mode="lost", photo=False, lost_report_id=str(report.id))

    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    assert [(r["candidate_type"], r["candidate_id"]) for r in body["results"]] == [
        ("sighting", sighting.id)
    ]
    extra = body["results"][0]["sighting"]
    assert extra["seen_place"] == "Near San Jose church"
    assert extra["contact_phone"] is None  # in-app only
    assert f"lost_report_id={report.id}" in extra["message_url"]
    assert db.session.scalar(sa.select(sa.func.count(SightingAccessLog.id))) == 1
    html = client.get(body["url"]).get_data(as_text=True)
    assert "Near San Jose church" in html and "Message finder" in html


def test_real_engine_found_mode_ignores_sightings(client, user, make_sighting, fake_gemini, login):
    make_sighting(seen_place="Should never appear")
    login(client, user)
    body = post_scan(client).get_json()
    assert body["results"] == []
    html = client.get(body["url"]).get_data(as_text=True)
    assert "Should never appear" not in html
    assert "No similar lost-pet reports" in html
